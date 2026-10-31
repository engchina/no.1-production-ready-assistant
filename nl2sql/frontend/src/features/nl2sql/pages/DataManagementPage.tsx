import { WarningsBanner } from "@/components/WarningsBanner";
import { DbProfileListSyncNotice } from "../components/DbProfileListSyncNotice";
import { useDbProfileListSync, type DbProfileListSync } from "../useDbProfileListSync";
import { useWorkspaceActive, useWorkspaceState, useWorkspaceRevalidation, useResetExecutionConsent, useTransientDraftGuard, useWorkspaceUrlSelection } from "@/components/WorkspaceState";
import { syntheticRunPollingInterval } from "../syntheticRunPolling";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useValuesChanged } from "@/lib/render-sync";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { ArrowRight, Database, Eye, FileSpreadsheet, Play, RefreshCw, Table2, Trash2, Upload, X } from "lucide-react";

import {
  compareNames,
  Button,
  ButtonLink,
  Banner,
  EmptyState,
  toast,
  StatusBadge,
  PageHeader,
  PageBody,
  ContentActionBar,
  ClearActionButton,
  ProcessingIndicator,
  ObjectActionBar,
  type EntityAction,
  ListPicker,
  type ListPickerItem,
  INFORMATION_TABLE_FIXED_VISIBLE_ROWS,
  ExecutionConfirmationField,
  FieldLegend,
  FormGrid,
  SelectField,
  TextareaField,
  TextField,
  useActionPending,
  FilterSelect,
  isFilterActive,
} from "@production-ready/ui";

import { SyntheticRunPanel, useSyntheticRuns, reviewExpired, type SyntheticRun } from "../syntheticRuns";
import { SelectAiCredentialPrecondition } from "../components/SelectAiCredentialPrecondition";
import { useSelectAiCredentialReadiness } from "../useSelectAiCredentialReadiness";
import { SyntheticReview } from "../SyntheticReview";

import { ErrorState } from "@/components/StateViews";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { apiFetch, apiGet, apiPost, isTimeoutError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { randomUuid } from "@/lib/randomUuid";
import { t } from "@/lib/i18n";
import { API_TIMEOUT_MS, requestTimeoutSeconds } from "@/lib/requestPolicy";
import { APP_ROUTES } from "@/lib/routes";
import { CORE_TABULAR_FILE_FORMATS } from "@/lib/tabular-file-formats";
import { selectedVisibleStringKey } from "@/lib/visible-selection";
import {
  DbAdminErrorNotice,
  downloadBlob,
  fileToBase64,
  runtimeLabel,
} from "../components/DbAdminShared";
import { QueryResultTable } from "../components/QueryResultTable";
import {
  DEFAULT_SQL_ROW_LIMIT,
  RowLimitField,
  parseSqlRowLimit,
} from "../components/SqlRowLimitControls";
import {
  DbManagementLoadingSkeleton,
  DropDbObjectDialog,
  DbObjectListDetail,
  DbObjectManagementPanelShell,
  DbObjectManagementTabs,
  DbObjectPanelHeader,
  DbObjectSelectionSummary,
  dbObjectListCount,
  DbObjectSelectorFooter,
  DbObjectSelectorToolbar,
  DbObjectStepIndicator,
  DbSingleObjectPickerList,
  dbAdminExecuteFailureMessage,
  compareDbObjectSortValues,
  dbObjectSortValue,
  formatDbObjectName,
  parseDbAdminObjectTarget,
  rowCountLabel,
  sortDbObjectPickerItems,
  type DbObjectPickerSortKey,
  type DbObjectPickerSortState,
  type DbObjectSortKey,
  type DbObjectSortState,
  type DbObjectTab,
  type DbObjectPickerItem,
} from "../components/DbObjectManagementShared";
import { DbObjectName } from "../components/DbObjectName";
import { formatDbAdminObjectPart } from "../dbObjectIdentity";
import { BUSINESS_SELECT_AI_DB_PROFILES_URL } from "../selectAiProfileUrls";
import { useSchemaRefreshCoordinator } from "../SchemaRefreshCoordinator";
import {
  SchemaRefreshHeaderStatus,
  SchemaRefreshProcessing,
  SchemaSyncNotice,
} from "../components/SchemaRefreshFeedback";
import { useMutationSchemaSync } from "../useMutationSchemaSync";
import {
  useDbAdminObjects,
} from "../incrementalQueries";
import { dbAdminObjectCountsFromPage } from "../dbAdminObjectCounts";
import { useNumberDraft } from "../useNumberDraft";
import type {
  DbAdminCsvUploadData,
  DbAdminDataPreviewData,
  DbAdminExecuteData,
  DbAdminObjectSummary,
  SelectAiDbProfile,
  SelectAiDbProfileDetailData,
  SelectAiDbProfilesData,
  SyntheticDataOperationData,
  SyntheticDataResultsData,
} from "../types";

type ActiveView = "preview" | "csv" | "synthetic";
type CsvStep = "file" | "execute";
type CsvMode = "insert" | "truncate_insert";
type PreviewObjectKind = "table" | "view";
type PreviewObjectKindFilter = "all" | PreviewObjectKind;
type SyntheticLoading = "" | "tables" | "generate" | "results";

const DATA_MANAGEMENT_ID = "data-management";
/** ヘッダーの「スキーマを更新」の起点（SchemaRefreshCoordinator の start(origin)。#821）。 */
const DATA_SCHEMA_REFRESH_HEADER = "data-management-header";
/** 切り詰めの後の案内の「DB 構造を再取得」の起点（#2031）。 */
const DATA_SCHEMA_REFRESH_NOTICE = "data-management-notice";
const DEFAULT_DATA_PREVIEW_ROW_LIMIT = DEFAULT_SQL_ROW_LIMIT;
const DEFAULT_SYNTHETIC_RESULT_LIMIT = DEFAULT_SQL_ROW_LIMIT;
const DEFAULT_OBJECT_PICKER_SORT: DbObjectPickerSortState = { key: "name", direction: "asc" };
const DEFAULT_PREVIEW_OBJECT_SORT: DbObjectSortState = { key: "name", direction: "asc" };
/**
 * データの表示で選んだ対象（`OWNER.NAME`）の URL の検索パラメータ（一覧 + 詳細。#2315）。
 * タブの 1 つだけが一覧 + 詳細なので、ほかのタブへ移るときに URL から外す（タブへ戻ると最後の選択を戻す）。
 */
const PREVIEW_OBJECT_PARAM = "object";

function resolveBusinessSelectAiProfileName(current: string, profiles: SelectAiDbProfile[]) {
  if (current && profiles.some((profile) => profile.name === current)) return current;
  return profiles[0]?.name ?? "";
}

function nextPreviewObjectSort(current: DbObjectSortState, key: DbObjectSortKey): DbObjectSortState {
  // 一覧の並べ替えは名前・行数の列見出し（狭い列で種類・所有者の列は持たない）。
  if (current.key === key) return { key, direction: current.direction === "asc" ? "desc" : "asc" };
  return { key, direction: "asc" };
}

function nextObjectPickerSort(
  current: DbObjectPickerSortState,
  key: DbObjectPickerSortKey
): DbObjectPickerSortState {
  if (current.key === key) {
    return { key, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { key, direction: "asc" };
}

/** 合成データの「生成」の aria-describedby が指す、Select AI の Credential の事前条件（#1688）。 */
const SYNTHETIC_CREDENTIAL_PRECONDITION_ID = "synthetic-credential-precondition";

export function DataManagementPage() {
  const queryClient = useQueryClient();
  useWorkspaceRevalidation();
  const workspaceActive = useWorkspaceActive();
  const [activeView, setActiveView] = useWorkspaceState<ActiveView>("activeView", "preview");
  const [previewObjectSearch, setPreviewObjectSearch] = useWorkspaceState("previewObjectSearch", "");
  const [previewObjectOwnerPrefix, setPreviewObjectOwnerPrefix] = useWorkspaceState("previewObjectOwnerPrefix", "");
  const [previewObjectKindFilter, setPreviewObjectKindFilter] = useWorkspaceState<PreviewObjectKindFilter>("previewObjectKindFilter", "all");
  const [previewObjectSort, setPreviewObjectSort] = useWorkspaceState<DbObjectSortState>("previewObjectListSort", DEFAULT_PREVIEW_OBJECT_SORT);
  const [previewRowLimitInput, setPreviewRowLimitInput] = useWorkspaceState("previewRowLimitInput", String(DEFAULT_DATA_PREVIEW_ROW_LIMIT));
  const [preview, setPreview] = useState<DbAdminDataPreviewData | null>(null);
  const [executedPreviewRowLimit, setExecutedPreviewRowLimit] = useState<number | null>(null);
  const [csvTable, setCsvTable] = useState("");
  const [csvTableSearch, setCsvTableSearch] = useWorkspaceState("csvTableSearch", "");
  const [csvTableSort, setCsvTableSort] = useState<DbObjectPickerSortState>(DEFAULT_OBJECT_PICKER_SORT);
  const [csvFilename, setCsvFilename] = useState("");
  const [csvFileResetSignal, setCsvFileResetSignal] = useState(0);
  const [csvBase64, setCsvBase64] = useState("");
  const [csvMode, setCsvMode] = useWorkspaceState<CsvMode>("csvMode", "insert");
  const [csvStep, setCsvStep] = useState<CsvStep>("file");
  const [csvConfirmation, setCsvConfirmation] = useState("");
  const [csvUploadResult, setCsvUploadResult] = useState<DbAdminCsvUploadData | null>(null);
  useTransientDraftGuard(Boolean(csvBase64) && !csvUploadResult?.executed);
  const syntheticRuns = useSyntheticRuns({ enabled: workspaceActive });
  const [searchParams, setSearchParams] = useSearchParams();
  // データの表示で選んだ対象は URL の `?object=` が唯一の情報源（一覧 + 詳細。#2315）。先頭の行を自動で選ばない。
  const previewSelection = useWorkspaceUrlSelection(PREVIEW_OBJECT_PARAM, { enabled: activeView === "preview" });
  const previewObject = previewSelection.selectedId ?? "";
  // 対象を付けたリンク（`?object=`）で開いたときは、ほかのタブを開いていてもデータの表示を開く。
  const linkedPreviewObject = useRef(Boolean(searchParams.get(PREVIEW_OBJECT_PARAM)));
  useEffect(() => {
    if (!linkedPreviewObject.current) return;
    linkedPreviewObject.current = false;
    setActiveView("preview");
  }, [setActiveView]);
  const changeActiveView = (view: ActiveView) => {
    if (view === activeView) return;
    if (activeView === "preview" && searchParams.has(PREVIEW_OBJECT_PARAM)) {
      // 選択は作業状態に残したまま URL から外す（ほかのタブの URL に対象を残さない。戻ると URL に戻る）。
      setSearchParams((params) => { params.delete(PREVIEW_OBJECT_PARAM); return params; }, { replace: true });
    }
    setActiveView(view);
  };
  const [syntheticRunId, setSyntheticRunId] = useWorkspaceState("syntheticRunId", "");
  const [submissionKey, setSubmissionKey] = useWorkspaceState("syntheticSubmission", { signature: "", key: "" });
  const requestedRunId = searchParams.get("synthetic_run") || syntheticRunId;
  const listedRun = requestedRunId ? syntheticRuns.data?.find((r) => r.run_id === requestedRunId) : syntheticRuns.data?.[0];
  const historicalRun = useQuery({
    queryKey: [...syntheticRuns.key, requestedRunId],
    queryFn: ({ signal }) => apiGet<SyntheticRun>(`/api/nl2sql/synthetic-data/runs/${encodeURIComponent(requestedRunId)}`, { signal, timeoutMs: API_TIMEOUT_MS.interactiveDetail }),
    enabled: workspaceActive && Boolean(requestedRunId) && !listedRun && syntheticRuns.isSuccess,
    refetchInterval: syntheticRunPollingInterval,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    retry: false,
  });
  const candidateRun = listedRun ?? (requestedRunId ? historicalRun.data : null) ?? null;
  // 生成の記録は期間で消さないので、確認用データの期間を過ぎた記録も選んで読める（#3235）。
  const selectedRun = candidateRun;
  const [syntheticQueryTime, setSyntheticQueryTime] = useState("");
  const resultRequest = useRef(0);
  const [resultGeneratedRows, setResultGeneratedRows] = useState<number | null>(null);
  const autoPreviewed = useRef(new Set<string>());
  const resultSelectionEdited = useRef(false);
  const [syntheticData, setSyntheticData] = useState<SyntheticDataOperationData | null>(null);
  const [syntheticDataResults, setSyntheticDataResults] = useState<SyntheticDataResultsData | null>(null);
  const [viewedPreviews, setViewedPreviews] = useState<Record<string, string>>({});
  const [syntheticProfileName, setSyntheticProfileName] = useState("");
  const [syntheticAvailableTables, setSyntheticAvailableTables] = useState<string[]>([]);
  const [syntheticSelectedTables, setSyntheticSelectedTables] = useState<string[]>([]);
  const [syntheticPrompt, setSyntheticPrompt] = useWorkspaceState("syntheticPrompt", "");
  const [syntheticConfirmation, setSyntheticConfirmation] = useState("");
  const [syntheticRows, setSyntheticRows] = useWorkspaceState("syntheticRows", 1);
  const [syntheticSampleRows, setSyntheticSampleRows] = useWorkspaceState("syntheticSampleRows", 5);
  const [syntheticUseComments, setSyntheticUseComments] = useWorkspaceState("syntheticUseComments", true);
  const [syntheticResultTable, setSyntheticResultTable] = useState("");
  const [syntheticResultLimitInput, setSyntheticResultLimitInput] = useState(String(DEFAULT_SYNTHETIC_RESULT_LIMIT));
  const [executedSyntheticResultLimit, setExecutedSyntheticResultLimit] = useState<number | null>(null);
  const [previewLoadingObject, setPreviewLoadingObject] = useState("");
  // 「表示」を押して始めたプレビューか。切り詰めの後の自動の表示・「再試行」ではボタンを回さず、
  // 結果の領域の読込表示がスピナーを出す（#819）。
  const [previewFromButton, setPreviewFromButton] = useState(false);
  const [truncateTargetName, setTruncateTargetName] = useState("");
  const [truncateConfirmation, setTruncateConfirmation] = useState("");
  const [truncateLoading, setTruncateLoading] = useState(false);
  const [truncateError, setTruncateError] = useState("");
  useResetExecutionConsent(() => { setCsvConfirmation(""); setSyntheticConfirmation(""); setTruncateConfirmation(""); setTruncateTargetName(""); }, JSON.stringify([csvTable, csvBase64, csvMode, syntheticProfileName, syntheticSelectedTables, syntheticPrompt, syntheticRows]));
  const [previewError, setPreviewError] = useState("");
  const [exportLoading, setExportLoading] = useState(false);
  const [exportError, setExportError] = useState("");
  const [csvUploading, setCsvUploading] = useState(false);
  const [csvUploadError, setCsvUploadError] = useState("");
  const [syntheticLoading, setSyntheticLoading] = useState<SyntheticLoading>("");
  // 「テーブル再取得」を押して始めた取得か。失敗の「再試行」からの取得ではボタンを回さない（#819）。
  const [syntheticTablesFromButton, setSyntheticTablesFromButton] = useState(false);
  // ヘッダーの「最新情報を取得」を押した取り直しの間だけ true（切り詰め・取込の後の取り直しでは回さない。#819）。
  const manualRefresh = useActionPending();
  const [syntheticError, setSyntheticError] = useState("");
  const [syntheticErrorOperation, setSyntheticErrorOperation] = useState<SyntheticLoading>("");
  const previewRequestSequence = useRef(0);
  const csvTableManualSelection = useRef(false);
  // 検索語・所有者の接頭辞は SearchField が確定した値（入力が止まって 300ms・Enter・消去。IME の変換中は
  // 確定しない）なので、ここでは遅延させずにそのまま問い合わせに使う（#535）。
  const baseObjectsQuery = useDbAdminObjects("", "all", "all");
  const csvTablesQuery = useDbAdminObjects(csvTableSearch, "table", "all");
  const previewObjectsQuery = useDbAdminObjects(
    previewObjectSearch,
    previewObjectKindFilter,
    "all",
    previewObjectOwnerPrefix,
    "name_comment"
  );
  const sharedSchemaRefresh = useSchemaRefreshCoordinator();
  const selectAiProfilesQuery = useQuery({
    queryKey: ["nl2sql", "select-ai", "db-profiles", "business"],
    queryFn: ({ signal }) =>
      apiGet<SelectAiDbProfilesData>(BUSINESS_SELECT_AI_DB_PROFILES_URL, {
        signal,
        timeoutMs: API_TIMEOUT_MS.interactiveList,
      }),
    enabled: activeView === "synthetic",
    staleTime: 5_000,
    retry: false,
  });
  const selectAiDbProfiles = selectAiProfilesQuery.data ?? null;
  // DB Profile 一覧の読み取りモデルが未初期化なら、「対象」の欄に再取得を求める案内を 1 か所だけ出す。押すと
  // 進行は同じ案内の中のボタンの直下に出し、再取得が終わって一覧を取り直したら案内を消す（構造化した
  // `profile_list_refresh_required` を正本にし、警告の文で判定しない。messaging §10.2、#2034）。
  const { refetch: refetchSelectAiProfiles } = selectAiProfilesQuery;
  const dbProfileListRequired = Boolean(selectAiDbProfiles?.profile_list_refresh_required);
  const dbProfileListSync = useDbProfileListSync({
    onDone: () => void refetchSelectAiProfiles(),
    required: dbProfileListRequired
      ? {
          // 取り直した一覧がまだ未初期化なら、案内を出し直す。
          key: String(selectAiProfilesQuery.dataUpdatedAt),
          notice: {
            severity: "warning",
            title: t("dataTools.syntheticData.dbProfileRefreshRequiredTitle"),
            message: t("dataTools.syntheticData.dbProfileRefreshRequiredHint"),
            errorCode: "",
          },
        }
      : null,
  });
  const dbProfileRefreshing = dbProfileListSync.busy;
  // 再取得の完了の後、一覧を取り直すまでの間は案内を出さない（解消した案内を出し直さない）。
  const dbProfileRefreshRequired = dbProfileListRequired && !dbProfileListSync.requiredResolved;
  const baseObjectPages = baseObjectsQuery.data?.pages ?? [];
  const previewObjectItems = useMemo<DbAdminObjectSummary[]>(
    () => (previewObjectsQuery.data?.pages ?? []).flatMap((page) => page.items),
    [previewObjectsQuery.data]
  );
  const firstPreviewObjectPage = previewObjectsQuery.data?.pages[0];
  const previewObjectCounts = dbAdminObjectCountsFromPage(firstPreviewObjectPage, previewObjectItems);
  const csvTableItems = useMemo(
    () =>
      (csvTablesQuery.data?.pages ?? []).flatMap((page) =>
        page.items.filter((item) => item.object_type === "table")
      ),
    [csvTablesQuery.data]
  );
  const firstCsvTablePage = csvTablesQuery.data?.pages[0];
  const csvTableCount = dbAdminObjectCountsFromPage(firstCsvTablePage, csvTableItems).totalCount;
  // 絞り込む前の全件（「N 件（全 M 件）」の M。#1891）。検索語・種類の無いオブジェクトの一覧（baseObjectsQuery）の
  // total・table_count を使う（同じ画面で常に読んでいる問い合わせなので、追加の要求は出ない）。
  const baseObjectCounts = baseObjectsQuery.data
    ? dbAdminObjectCountsFromPage(
        baseObjectPages[0],
        baseObjectPages.flatMap((page) => page.items)
      )
    : undefined;

  // 一覧の行（名前とコメントの 2 段 + 行数・種類）。並べ替えは名前・行数の列見出し（#2315）。
  const sortedPreviewObjects = useMemo(() => {
    const filtered = filterPreviewObjects(
      previewObjectItems,
      previewObjectSearch,
      previewObjectOwnerPrefix,
      previewObjectKindFilter
    );
    const sortKey: DbObjectSortKey = previewObjectSort.key === "row_count" ? "row_count" : "name";
    return [...filtered].sort((left, right) => {
      return compareDbObjectSortValues(
        dbObjectSortValue(left, sortKey),
        dbObjectSortValue(right, sortKey),
        previewObjectSort.direction
      );
    });
  }, [previewObjectItems, previewObjectKindFilter, previewObjectOwnerPrefix, previewObjectSearch, previewObjectSort]);
  const selectedPreviewObjectItem = useMemo(
    () => previewObjectItems.find((item) => formatDbObjectName(item) === previewObject),
    [previewObject, previewObjectItems]
  );
  const parsedPreviewRowLimit = parseSqlRowLimit(previewRowLimitInput);
  const previewRowLimitError =
    parsedPreviewRowLimit === null ? t("queryResults.rowLimit.error") : "";
  const canShowPreview =
    Boolean(previewObject) && !previewLoadingObject && parsedPreviewRowLimit !== null;
  const csvTablePickerItems = useMemo(
    () =>
      sortDbObjectPickerItems(
        csvTableItems.map<DbObjectPickerItem>((item) => ({
          key: formatDbObjectName(item),
          name: formatDbObjectName(item),
          kind: "table",
          owner: item.owner,
          comment: item.comment,
          kindLabel: t("dataMgmt.preview.kindFilterTable"),
          kindVariant: "neutral",
          rowCount: item.row_count,
          rowCountLabel: rowCountLabel(item.row_count),
        })),
        csvTableSort
      ),
    [csvTableItems, csvTableSort]
  );

  const selectedSyntheticProfile = useMemo(
    () => selectAiDbProfiles?.profiles.find((profile) => profile.name === syntheticProfileName) ?? null,
    [selectAiDbProfiles, syntheticProfileName]
  );
  // 対象名確認: CSV は選択テーブル名、synthetic は単一テーブル指定時のみ対象名
  // (複数テーブル指定は単一対象名が無いため ADMIN_EXECUTE)。backend の検証と一致させる。
  const csvConfirmed = Boolean(csvTable.trim()) && csvConfirmation.trim() === csvTable.trim();
  const canUploadCsv = Boolean(csvTable && csvBase64 && csvConfirmed && !csvUploading);
  const canClearCsvUpload = Boolean(
    csvFilename ||
      csvBase64 ||
      csvConfirmation ||
      csvUploadResult ||
      csvUploadError ||
      csvMode !== "insert"
  );
  const syntheticExpectedConfirmation =
    syntheticSelectedTables.length === 1 ? syntheticSelectedTables[0] : "ADMIN_EXECUTE";
  const syntheticDataConfirmed = syntheticConfirmation.trim() === syntheticExpectedConfirmation;
  // 合成データは業務プロファイルの DBMS_CLOUD_AI Profile で作るので、Select AI の Credential が前提
  // （#1688）。無いと分かったら生成の操作の直前に事前条件を示し、生成を止める。
  const selectAiCredential = useSelectAiCredentialReadiness();
  const syntheticCredentialPrecondition = selectAiCredential.missing ? (
    <SelectAiCredentialPrecondition
      id={SYNTHETIC_CREDENTIAL_PRECONDITION_ID}
      testId="synthetic-credential-precondition"
      operation={t("selectAiCredential.precondition.operation.synthetic")}
          action={t("selectAiCredential.precondition.action.syntheticStart")}
          credentialName={selectAiCredential.credentialName}
      canOpenSettings={selectAiCredential.canOpenSettings}
      rechecking={selectAiCredential.rechecking}
      onRecheck={() => void selectAiCredential.recheck()}
    />
  ) : null;
  const canGenerateSyntheticData = Boolean(
    syntheticProfileName.trim() &&
      syntheticSelectedTables.length > 0 &&
      syntheticDataConfirmed &&
      !syntheticLoading &&
      !selectAiCredential.missing
  );
  // 生成条件のリセットは入力（と生成開始の失敗表示）だけを戻す。表示中の結果は「表示件数・結果をリセット」、
  // サーバーに残る生成（生成状況・確認・適用）は「確認用データを破棄」で扱う。
  const canClearSyntheticGeneration = Boolean(
    syntheticSelectedTables.length > 0 ||
      syntheticPrompt ||
      syntheticConfirmation ||
      (syntheticError && syntheticErrorOperation !== "results") ||
      syntheticRows !== 1 ||
      syntheticSampleRows !== 5 ||
      !syntheticUseComments
  );
  const syntheticResultLimit = parseSqlRowLimit(syntheticResultLimitInput);
  const syntheticResultLimitError =
    syntheticResultLimit === null ? t("queryResults.rowLimit.error") : "";
  const syntheticResultError = syntheticErrorOperation === "results" ? syntheticError : "";
  const syntheticWorkspaceError = syntheticErrorOperation === "results" ? "" : syntheticError;
  const canLoadSyntheticDataResults = Boolean(
    (!selectedRun?.preview || (["ready", "applied"].includes(selectedRun.review_status ?? "") && !reviewExpired(selectedRun))) &&
    (selectedRun ? selectedRun.targets.some((target) => target.table_name === syntheticResultTable) : syntheticAvailableTables.includes(syntheticResultTable)) && syntheticResultLimit !== null && !syntheticLoading
  );
  const canClearSyntheticDataResults = Boolean(
    syntheticDataResults ||
      syntheticResultError ||
      executedSyntheticResultLimit !== null ||
      syntheticResultLimitInput !== String(DEFAULT_SYNTHETIC_RESULT_LIMIT)
  );

  const clearSyntheticResultState = ({ resetLimit = false }: { resetLimit?: boolean } = {}) => {
    setSyntheticDataResults(null);
    setExecutedSyntheticResultLimit(null);
    if (resetLimit) setSyntheticResultLimitInput(String(DEFAULT_SYNTHETIC_RESULT_LIMIT));
    if (syntheticErrorOperation === "results") {
      setSyntheticError("");
      setSyntheticErrorOperation("");
    }
  };

  const clearSyntheticProfileTargets = () => {
    setSyntheticAvailableTables([]);
    setSyntheticSelectedTables([]);
    setSyntheticResultTable("");
    setSyntheticData(null);
    setSyntheticDataResults(null);
    setExecutedSyntheticResultLimit(null);
    setSyntheticResultLimitInput(String(DEFAULT_SYNTHETIC_RESULT_LIMIT));
    if (syntheticErrorOperation === "results") {
      setSyntheticError("");
      setSyntheticErrorOperation("");
    }
  };

  const changeSyntheticProfileName = (value: string) => {
    if (value === syntheticProfileName) return;
    setSyntheticProfileName(value);
    clearSyntheticProfileTargets();
  };

  // profile 一覧が変わったレンダーで、選択中の profile 名を直す（effect で setState しない）。
  const selectAiProfiles = selectAiProfilesQuery.data;
  const selectAiProfilesChanged = useValuesChanged([selectAiProfiles]);
  if (selectAiProfilesChanged && selectAiProfiles) {
    const nextName = resolveBusinessSelectAiProfileName(syntheticProfileName, selectAiProfiles.profiles);
    if (nextName !== syntheticProfileName) setSyntheticProfileName(nextName);
    if (syntheticProfileName && syntheticProfileName !== nextName) {
      clearSyntheticProfileTargets();
    }
  }

  useEffect(() => {
    const nextTable = selectedVisibleStringKey(csvTablePickerItems, csvTable, (item) => item.key, {
      preserveSelected: csvTableManualSelection.current,
    });
    if (nextTable === csvTable) return;
    csvTableManualSelection.current = false;
    setCsvTable(nextTable);
    setCsvUploadResult(null);
    setCsvStep("file");
  }, [csvTable, csvTablePickerItems]);

  // refetch は query の observer ごとに固定の関数なので、deps に入れても発火の条件は変わらない。
  const { refetch: refetchBaseObjects } = baseObjectsQuery;
  const { refetch: refetchCsvTables } = csvTablesQuery;
  const { refetch: refetchPreviewObjects } = previewObjectsQuery;
  // 切り詰めの後の DB 構造の同期の案内と進行は、ページの先頭の 1 か所に出す（切り詰めの確認は閉じるため。
  // 案内の中のボタンで始めた再取得の進行は、そのボタンの直下に出す。#2031）。
  const refetchObjectsAfterSchemaSync = useCallback(() => {
    void refetchBaseObjects();
    void refetchCsvTables();
    void refetchPreviewObjects();
  }, [refetchBaseObjects, refetchCsvTables, refetchPreviewObjects]);
  const schemaSync = useMutationSchemaSync({
    noticeOrigin: DATA_SCHEMA_REFRESH_NOTICE,
    onDone: refetchObjectsAfterSchemaSync,
  });

  const refreshObjects = async (announce = false) => {
    const results = await Promise.all([
      baseObjectsQuery.refetch(),
      csvTablesQuery.refetch(),
      previewObjectsQuery.refetch(),
    ]);
    if (announce && results.every((result) => !result.isError)) {
      toast.success(t("common.action.refreshed"));
    }
  };

  const resetPreviewResult = () => {
    previewRequestSequence.current += 1;
    setPreview(null);
    setExecutedPreviewRowLimit(null);
    setPreviewLoadingObject("");
    setPreviewError("");
    setExportError("");
  };

  // 選んだ対象のデータを今の取得件数上限で取得する。別の対象を選び直したときは前の取得の応答を捨てる（sequence）。
  const showPreview = async (objectName: string, options: { fromButton?: boolean } = {}) => {
    const rowLimit = parsedPreviewRowLimit;
    if (!objectName || rowLimit === null) return;
    const target = parseDbAdminObjectTarget(objectName);
    if (previewLoadingObject === target.qualifiedName) return;
    const sequence = previewRequestSequence.current + 1;
    previewRequestSequence.current = sequence;
    setPreview(null);
    setExecutedPreviewRowLimit(null);
    setPreviewLoadingObject(target.qualifiedName);
    setPreviewFromButton(Boolean(options.fromButton));
    setPreviewError("");
    setExportError("");
    try {
      const result = await apiPost<DbAdminDataPreviewData>(
        "/api/nl2sql/db-admin/preview-data",
        {
          object_name: target.name,
          owner: target.owner,
          limit: rowLimit,
          where_clause: "",
        },
        { timeoutMs: API_TIMEOUT_MS.interactiveDetail }
      );
      if (sequence !== previewRequestSequence.current) return;
      setPreview(result);
      setExecutedPreviewRowLimit(rowLimit);
    } catch (err) {
      if (sequence !== previewRequestSequence.current) return;
      setPreviewError(apiErrorMessage(err, "dataMgmt.error.preview"));
    } finally {
      if (sequence === previewRequestSequence.current) setPreviewLoadingObject("");
    }
  };

  // 選択（URL）が変わったら、その対象のデータを表示する（行を選ぶ・前へ / 次へ・再読込・リンク）。外れたら結果を消す。
  // 同じ対象のまま画面・タブへ戻ったときは取り直さない。最新の関数は ref から呼ぶ（条件の変わらないレンダーで判定し直さない）。
  const showPreviewRef = useRef(showPreview);
  useLayoutEffect(() => { showPreviewRef.current = showPreview; });
  const resetPreviewResultRef = useRef(resetPreviewResult);
  useLayoutEffect(() => { resetPreviewResultRef.current = resetPreviewResult; });
  const previewShownFor = useRef("");
  useEffect(() => {
    if (!workspaceActive || activeView !== "preview") return;
    if (previewShownFor.current === previewObject) return;
    previewShownFor.current = previewObject;
    if (!previewObject) {
      resetPreviewResultRef.current();
      return;
    }
    void showPreviewRef.current(previewObject);
  }, [workspaceActive, activeView, previewObject]);

  const openTruncateDialog = (objectName: string) => {
    setTruncateTargetName(objectName);
    setTruncateConfirmation("");
    setTruncateError("");
  };

  const closeTruncateDialog = () => {
    if (truncateLoading) return;
    setTruncateTargetName("");
    setTruncateConfirmation("");
    setTruncateError("");
  };

  const truncateTableData = async () => {
    const tableName = truncateTargetName;
    if (!tableName || truncateLoading) return;
    const target = parseDbAdminObjectTarget(tableName);
    setTruncateLoading(true);
    setTruncateError("");
    try {
      const result = await apiPost<DbAdminExecuteData>(
        "/api/nl2sql/db-admin/truncate-table",
        {
          table_name: target.name,
          owner: target.owner,
          confirmation: truncateConfirmation,
          reason: "ui-data-management-truncate",
        },
        { timeoutMs: API_TIMEOUT_MS.interactiveDetail }
      );
      if (!result.executed) {
        // Oracle のエラーは warnings ではなく文の error_message に入る（部分成功の経路）。
        // 1 文目に何が起きたか、続けて原因を出す（#936）。
        const cause = dbAdminExecuteFailureMessage(result, "");
        setTruncateError(cause ? `${t("dataMgmt.truncate.error")}${cause}` : t("dataMgmt.truncate.error"));
        return;
      }
      setTruncateTargetName("");
      setTruncateConfirmation("");
      toast.success(t("dataMgmt.truncate.success", { name: tableName }));
      await refreshObjects();
      schemaSync.track(result, { scope: "page" });
      await showPreview(tableName);
    } catch (err) {
      setTruncateError(apiErrorMessage(err, "dataMgmt.truncate.error"));
    } finally {
      setTruncateLoading(false);
    }
  };

  const downloadPreviewXlsx = async () => {
    const rowLimit = parsedPreviewRowLimit;
    if (!previewObject || rowLimit === null) return;
    const target = parseDbAdminObjectTarget(previewObject);
    setExportLoading(true);
    setExportError("");
    try {
      const response = await apiFetch("/api/nl2sql/db-admin/preview-data/export.xlsx", {
        method: "POST",
        headers: {
          Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          object_name: target.name,
          owner: target.owner,
          limit: rowLimit,
          where_clause: "",
        }),
        signal: AbortSignal.timeout(API_TIMEOUT_MS.interactiveDetail),
      });
      if (!response.ok) {
        throw new Error(await previewExportError(response));
      }
      const filename = `${target.qualifiedName.toLowerCase().replace(".", "_")}_preview.xlsx`;
      downloadBlob(filename, await response.blob());
      toast.success(t("common.action.downloaded"));
    } catch (err) {
      setExportError(apiErrorMessage(err, "dataMgmt.error.previewExport"));
    } finally {
      setExportLoading(false);
    }
  };

  const updatePreviewRowLimitInput = (value: string) => {
    setPreviewRowLimitInput(value);
    setPreview(null);
    setExecutedPreviewRowLimit(null);
    setPreviewError("");
    setExportError("");
  };

  const csvReadSequence = useRef(0);
  useEffect(() => {
    return () => { csvReadSequence.current += 1; };
  }, []);

  const pickCsvFile = async (file: File | undefined) => {
    if (!file) return;
    const sequence = ++csvReadSequence.current;
    setCsvFilename(file.name);
    setCsvConfirmation("");
    setCsvBase64("");
    setCsvUploadError("");
    setCsvUploadResult(null);
    setCsvStep("file");
    try {
      const content = await fileToBase64(file);
      if (sequence === csvReadSequence.current) setCsvBase64(content);
    } catch {
      if (sequence === csvReadSequence.current) setCsvUploadError(t("dbAdmin.runner.fileErrorRead"));
    }
  };

  const clearCsvFile = () => {
    csvReadSequence.current += 1;
    setCsvConfirmation("");
    setCsvFilename("");
    setCsvBase64("");
    setCsvUploadResult(null);
    setCsvStep("file");
    setCsvFileResetSignal((value) => value + 1);
  };

  const clearCsvUpload = () => {
    clearCsvFile();
    setCsvMode("insert");
    setCsvConfirmation("");
    setCsvUploadError("");
  };

  const uploadCsv = async () => {
    if (!canUploadCsv) return;
    const target = parseDbAdminObjectTarget(csvTable);
    setCsvUploading(true);
    setCsvUploadError("");
    try {
      const result = await apiPost<DbAdminCsvUploadData>("/api/nl2sql/db-admin/upload-csv", {
        table_name: target.name,
        owner: target.owner,
        content_base64: csvBase64,
        filename: csvFilename || "upload.csv",
        mode: csvMode,
        confirmation: csvConfirmation,
        reason: "ui-data-management-csv",
      }, { timeoutMs: API_TIMEOUT_MS.interactiveDetail });
      setCsvUploadResult(result);
      setCsvStep("execute");
      if (result.executed) {
        toast.success(t("dataMgmt.csv.successToast"));
        await baseObjectsQuery.refetch();
        await csvTablesQuery.refetch();
        await previewObjectsQuery.refetch();
      }
    } catch (err) {
      setCsvUploadError(apiErrorMessage(err, "dataMgmt.error.csvUpload"));
    } finally {
      setCsvUploading(false);
    }
  };

  const refreshSyntheticTables = async (fromButton = false) => {
    const profileName = syntheticProfileName.trim();
    if (!profileName) return;
    setSyntheticLoading("tables");
    setSyntheticTablesFromButton(fromButton);
    setSyntheticError("");
    setSyntheticErrorOperation("");
    try {
      let profile = selectedSyntheticProfile;
      try {
        const detail = await apiGet<SelectAiDbProfileDetailData>(
          `/api/nl2sql/select-ai/db-profiles/${encodeURIComponent(profileName)}`,
          { timeoutMs: API_TIMEOUT_MS.interactiveDetail }
        );
        profile = detail.profile;
      } catch {
        profile = selectedSyntheticProfile;
      }
      const profileTables = profileObjectNames(profile);
      const nextTables = profileTables;
      setSyntheticAvailableTables(nextTables);
      setSyntheticSelectedTables((current) => current.filter((tableName) => nextTables.includes(tableName)));
      setSyntheticResultTable((current) => (current && nextTables.includes(current) ? current : (nextTables[0] ?? "")));
      setSyntheticData(null);
      clearSyntheticResultState();
      toast.success(t("dataTools.syntheticData.toast.tablesLoaded", { count: nextTables.length }));
    } catch (err) {
      setSyntheticError(apiErrorMessage(err, "dataTools.error.load"));
      setSyntheticErrorOperation("tables");
    } finally {
      setSyntheticLoading("");
    }
  };

  const generateSyntheticData = async () => {
    if (!canGenerateSyntheticData) return;
    const selectedTables = syntheticSelectedTables;
    const singleTable = selectedTables.length === 1;
    setSyntheticLoading("generate");
    setSyntheticData(null);
    setSyntheticError("");
    setSyntheticErrorOperation("");
    try {
      clearSyntheticResultState();
      const body = {
        table_name: singleTable ? selectedTables[0] : "", object_list: singleTable ? [] : selectedTables,
        row_count: syntheticRows, rows_per_table: syntheticRows, profile_name: syntheticProfileName,
        user_prompt: syntheticPrompt, sample_rows: syntheticSampleRows, use_comments: syntheticUseComments,
        reason: "ui-synthetic-data",
      };
      const signature = JSON.stringify(body);
      const key = submissionKey.signature === signature ? submissionKey.key : randomUuid();
      setSubmissionKey({ signature, key });
      const run = await apiPost<SyntheticRun>("/api/nl2sql/synthetic-data/runs", {
        ...body, confirmation: syntheticConfirmation, idempotency_key: key,
      }, { timeoutMs: API_TIMEOUT_MS.interactiveDetail });
      queryClient.setQueryData<SyntheticRun[]>(syntheticRuns.key, (old = []) => [run, ...old.filter((r) => r.run_id !== run.run_id)]);
      setSyntheticRunId(run.run_id);
      setSearchParams((params) => { params.delete("synthetic_run"); return params; }, { replace: true });
      setSyntheticConfirmation("");
      setSubmissionKey({ signature: "", key: "" });
      resultSelectionEdited.current = false;
      setSyntheticResultTable(run.targets[0]?.table_name ?? "");
      toast.info(t("syntheticRun.accepted"));
    } catch (err) {
      setSyntheticError(apiErrorMessage(err, "dataTools.error.syntheticData", "syntheticRun.submitUnknown"));
      setSyntheticErrorOperation("generate");
      selectAiCredential.refreshAfterError(err);
    } finally {
      setSyntheticLoading("");
    }
  };

  const clearSyntheticGeneration = () => {
    setSubmissionKey({ signature: "", key: "" });
    setSyntheticSelectedTables([]);
    setSyntheticPrompt("");
    setSyntheticConfirmation("");
    setSyntheticRows(1);
    setSyntheticSampleRows(5);
    setSyntheticUseComments(true);
    if (syntheticErrorOperation !== "results") {
      setSyntheticError("");
      setSyntheticErrorOperation("");
    }
  };

  const loadSyntheticDataResults = async () => {
    const tableName = syntheticResultTable.trim();
    const rowLimit = syntheticResultLimit;
    if (!tableName || !(selectedRun ? selectedRun.targets.some((target) => target.table_name === tableName) : syntheticAvailableTables.includes(tableName)) || rowLimit === null) return;
    const requestVersion = ++resultRequest.current;
    const generatedRows = selectedRun?.targets.find((target) => target.table_name === tableName)?.loaded_rows ?? null;
    setSyntheticLoading("results");
    setSyntheticError("");
    setSyntheticErrorOperation("");
    setSyntheticDataResults(null);
    setExecutedSyntheticResultLimit(null);
    try {
      const result = await apiGet<SyntheticDataResultsData>(
        `/api/nl2sql/synthetic-data/${selectedRun ? `runs/${selectedRun.run_id}/results` : "results"}?table_name=${encodeURIComponent(tableName)}&limit=${rowLimit}`,
        { timeoutMs: API_TIMEOUT_MS.interactiveDetail }
      );
      if (requestVersion !== resultRequest.current) return;
      setResultGeneratedRows(generatedRows);
      setSyntheticDataResults(result);
      if (result.preview_digest && result.run_id === selectedRun?.run_id) {
        const checksum = result.preview_digest;
        setViewedPreviews(current => ({ ...current, [tableName]: checksum }));
      }
      setSyntheticQueryTime(new Date().toISOString());
      setExecutedSyntheticResultLimit(rowLimit);
      if (!result.preview_digest) toast.success(t("dataTools.syntheticData.toast.resultsLoaded", { name: result.table_name }));
    } catch (err) {
      if (requestVersion !== resultRequest.current) return;
      setSyntheticError(apiErrorMessage(err, "dataTools.error.syntheticResults"));
      setSyntheticErrorOperation("results");
    } finally {
      if (requestVersion === resultRequest.current) setSyntheticLoading("");
    }
  };

  // 選択中の run が変わったレンダーで、結果・確認済みの preview・run の表示を直す（effect で setState しない）。
  // 古い結果の応答は effect で捨てる。
  const selectedRunIdChanged = useValuesChanged([selectedRun?.run_id]);
  if (selectedRunIdChanged) {
    setSyntheticDataResults(null);
    setSyntheticLoading((value) => value === "results" ? "" : value);
  }
  // run が実際に変わったときだけ数える。同じ run で effect が再実行されたとき（StrictMode の mount 時の再実行など）に
  // 数えると、同じ commit で下の自動表示が始めた読み込みの応答まで捨て、閲覧済みの preview が記録されない（#342）。
  const resultRequestRunId = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (resultRequestRunId.current === selectedRun?.run_id) return;
    resultRequestRunId.current = selectedRun?.run_id;
    resultRequest.current += 1;
  }, [selectedRun?.run_id]);

  const viewedPreviewsStale = useValuesChanged([selectedRun?.run_id, workspaceActive]);
  if (viewedPreviewsStale) setViewedPreviews({});

  const selectedRunChanged = useValuesChanged([selectedRun?.run_id, selectedRun?.status]);
  if (selectedRunChanged && selectedRun) {
    if (searchParams.has("synthetic_run")) setActiveView("synthetic");
    setSyntheticRunId(selectedRun.run_id);
    setSyntheticData({ table_name: selectedRun.targets[0]?.table_name ?? "", object_list: selectedRun.targets.map((target) => target.table_name), row_count: 0, status: selectedRun.status, executed: selectedRun.status === "completed", runtime: "oracle", message: selectedRun.message, warnings: [], engine_meta: {}, timing: { created_at: selectedRun.created_at, stage_timings: [] } });
    setSyntheticResultTable((current) => selectedRun.targets.some((target) => target.table_name === current) ? current : selectedRun.targets[0]?.table_name ?? "");
  }

  // 自動の結果表示は run・表示の条件が変わったときだけ判定する。読み込みは最新の関数を ref から呼ぶ
  // （毎レンダー作り直す関数を deps に入れると、条件の変わらないレンダーでも判定し直してしまう）。
  const loadSyntheticDataResultsRef = useRef(loadSyntheticDataResults);
  useLayoutEffect(() => { loadSyntheticDataResultsRef.current = loadSyntheticDataResults; });
  const selectedRunId = selectedRun?.run_id;
  const selectedRunStatus = selectedRun?.status;
  useEffect(() => {
    if (selectedRunId === undefined || selectedRunStatus === undefined || !["completed", "partial"].includes(selectedRunStatus) || autoPreviewed.current.has(selectedRunId) || activeView !== "synthetic" || !workspaceActive || resultSelectionEdited.current || !canLoadSyntheticDataResults) return;
    autoPreviewed.current.add(selectedRunId);
    void loadSyntheticDataResultsRef.current();
  }, [selectedRunId, selectedRunStatus, canLoadSyntheticDataResults, activeView, workspaceActive]);

  const previewObjectErrorMessage =
    previewObjectsQuery.error && !previewObjectsQuery.data
      ? objectListErrorMessage(previewObjectsQuery.error)
      : "";
  const previewObjectLoadMoreError =
    previewObjectsQuery.isFetchNextPageError && previewObjectsQuery.error
      ? objectListLoadMoreErrorMessage(previewObjectsQuery.error)
      : "";
  const csvTablesErrorMessage =
    csvTablesQuery.error && !csvTablesQuery.data
      ? objectListErrorMessage(csvTablesQuery.error)
      : "";
  const csvTablesLoadMoreError =
    csvTablesQuery.isFetchNextPageError && csvTablesQuery.error
      ? objectListLoadMoreErrorMessage(csvTablesQuery.error)
      : "";
  const firstObjectPage = baseObjectPages[0];
  const schemaRefreshing = sharedSchemaRefresh.isRefreshing;
  const headerSchemaRefreshStarting = sharedSchemaRefresh.startingOrigin === DATA_SCHEMA_REFRESH_HEADER;
  const objectRefreshing =
    ((baseObjectsQuery.isFetching && !baseObjectsQuery.isFetchingNextPage) ||
      (previewObjectsQuery.isFetching && !previewObjectsQuery.isFetchingNextPage) ||
      (csvTablesQuery.isFetching && !csvTablesQuery.isFetchingNextPage)) &&
    Boolean(baseObjectsQuery.data || previewObjectsQuery.data || csvTablesQuery.data);
  const objectRefreshingFromHeader =
    (baseObjectsQuery.isFetching && !baseObjectsQuery.isFetchingNextPage) ||
    (previewObjectsQuery.isFetching && !previewObjectsQuery.isFetchingNextPage) ||
    (csvTablesQuery.isFetching && !csvTablesQuery.isFetchingNextPage);
  // ヘッダーの「最新情報を取得」は、押した再読込の間だけ回す（manualRefresh。#819）。初回の読込（一覧がまだ無い）は
  // 一覧の Skeleton がスピナーを出し、ボタンは無効にする（このボタンは狭い画面では「その他の操作」の中で見えない。
  // 同じ処理のスピナーは 1 つ。messaging §3.7、#416）。
  const objectReloadingWithContent = [baseObjectsQuery, previewObjectsQuery, csvTablesQuery].some(
    (query) => Boolean(query.data) && query.isFetching && !query.isFetchingNextPage
  );

  const syntheticProgress = (
    <>
      <SyntheticRunPanel
        run={selectedRun}
        runs={selectedRun && !listedRun ? [selectedRun, ...syntheticRuns.data ?? []] : syntheticRuns.data ?? []}
        submitting={syntheticLoading === "generate"}
        error={Boolean(syntheticRuns.error || historicalRun.error)}
        onSelect={(id) => {
          setSyntheticRunId(id);
          setSearchParams((params) => { params.delete("synthetic_run"); return params; }, { replace: true });
          clearSyntheticResultState();
          resultSelectionEdited.current = false;
        }}
        onRefresh={async () => {
          const [list, history] = await Promise.all([
            syntheticRuns.refetch({ cancelRefetch: false }),
            requestedRunId && !listedRun ? historicalRun.refetch({ cancelRefetch: false }) : Promise.resolve(null),
          ]);
          if (list.isError || history?.isError) throw new Error("synthetic run refresh failed");
          const latest = list.data?.find((item) => item.run_id === selectedRun?.run_id) ?? history?.data ?? null;
          if (selectedRun && !latest) throw new Error("synthetic run missing");
          return latest;
        }}
      />
      {historicalRun.error && requestedRunId && !listedRun && (
        <Banner severity="warning">{apiErrorMessage(historicalRun.error, "syntheticRun.missing")}</Banner>
      )}
    </>
  );

  return (
    <>
      <PageHeader wide
        title={t("nav.dataManagement")}
        subtitle={t("dataMgmt.subtitle")}
        meta={
          firstObjectPage?.refreshed_at
            ? t("common.schemaRefreshedAt", {
                date: formatDateTime(firstObjectPage.refreshed_at),
              })
            : undefined
        }
        status={<SchemaRefreshHeaderStatus testId="data-management-schema-refresh-status" />}
        // 1 ペイン（xl 未満）のデータの表示の詳細の「一覧へ戻る」はページの左上（#2281・#2315）。
        back={
          activeView === "preview" && !previewSelection.split && previewObject
            ? { label: t("dataMgmt.preview.backToList"), onClick: previewSelection.clear, testId: "data-management-detail-back" }
            : undefined
        }
        actions={[
          {
            id: "refresh-data",
            kind: "utility",
            label: t("common.action.refresh"),
            icon: RefreshCw,
            // 押した再読込の間だけ回す。切り詰め・取込の後の取り直しでは回さず、下の処理中の表示が
            // スピナーを出す（#819）。
            loading: manualRefresh.pending,
            disabled: objectRefreshingFromHeader && !objectReloadingWithContent,
            onClick: () => void manualRefresh.track(() => refreshObjects(true)),
          },
          {
            id: "refresh-data-schema",
            kind: "utility",
            label: t("common.action.schemaRefresh"),
            icon: RefreshCw,
            // 押したときの送信の間だけ回し、job の間は無効にするだけ（スピナーは進行の表示。#821）。
            loading: headerSchemaRefreshStarting,
            disabled: schemaRefreshing && !headerSchemaRefreshStarting,
            onClick: () => void schemaSync.refreshFrom(DATA_SCHEMA_REFRESH_HEADER),
          },
        ]}
        actionsTestId="data-management-actions"
      />
      <PageBody wide className="grid gap-4">
        <SchemaSyncNotice
          sync={schemaSync}
          scope="page"
          testId="data-management-schema-sync-notice"
          processingTestId="data-management-schema-sync-processing"
        />
        {schemaRefreshing && !schemaSync.ownsProgress ? (
          <SchemaRefreshProcessing testId="data-management-workspace-processing" />
        ) : objectRefreshing ? (
          <ProcessingIndicator
            active
            label={t("common.processing.refreshing")}
            operationKey="object-refresh"
            placement="workspace"
            className="rounded-md border border-border bg-surface-sunken px-3 py-2"
            testId="data-management-workspace-processing"
            activityIcon={manualRefresh.pending ? "none" : "spinner"}
          />
        ) : null}

        <DbObjectManagementTabs
          activeView={activeView}
          tabs={[
            { id: "preview", label: t("dataMgmt.preview.title"), icon: Table2 },
            { id: "csv", label: t("dataMgmt.csv.title"), icon: Upload },
            { id: "synthetic", label: t("dataTools.synthetic.title"), icon: Database },
          ] satisfies Array<DbObjectTab<ActiveView>>}
          idPrefix={DATA_MANAGEMENT_ID}
          ariaLabel={t("dataMgmt.tabs.label")}
          onViewChange={changeActiveView}
        />

        {activeView === "preview" && (
          <DbObjectManagementPanelShell
            id="data-management-panel-preview"
            labelledBy="data-management-tab-preview"
            idPrefix={DATA_MANAGEMENT_ID}
            ariaLabel={t("dataMgmt.workspace.preview")}
            framed={false}
          >
            <DbObjectListDetail
              showComments
              idPrefix="data-management-preview"
              headingId="data-preview-list-heading"
              icon={Table2}
              items={sortedPreviewObjects}
              selectedName={previewObject}
              loading={previewObjectsQuery.isPending && !previewObjectsQuery.data}
              error={previewObjectErrorMessage}
              search={previewObjectSearch}
              ownerPrefix={previewObjectOwnerPrefix}
              sort={previewObjectSort}
              totalCount={previewObjectCounts.totalCount}
              unfilteredTotalCount={baseObjectCounts?.totalCount}
              hasNextPage={Boolean(previewObjectsQuery.hasNextPage)}
              loadingNextPage={previewObjectsQuery.isFetchingNextPage}
              loadMoreError={previewObjectLoadMoreError}
              labels={{
                title: t("dataMgmt.preview.object"),
                hint: t("dataMgmt.preview.controlsHint"),
                loading: t("dataMgmt.objectList.loading"),
                emptyTitle: t("dataMgmt.preview.emptyObjectsTitle"),
                emptyHint: t("dataMgmt.preview.emptyObjectsHint"),
                noResultsTitle: t("dataMgmt.preview.noObjectsTitle"),
                noResultsHint: t("dataMgmt.preview.noObjectsHint"),
                objectName: t("dataMgmt.preview.objectName"),
                rows: t("dataMgmt.preview.objectRows"),
                owner: t("dataMgmt.preview.objectOwner"),
                showObject: (name) => t("dataMgmt.preview.selectObject", { name }),
              }}
              kindLabel={(item) => previewObjectKindLabel(item.object_type === "view" ? "view" : "table")}
              formatRowCount={previewObjectRowCountLabel}
              // 検索・所有者・種別の 3 つ。狭い列（28rem 未満）では「絞り込み（N）」のシートにまとめる（#1933）。
              filtersLayout="sheet"
              filters={
                <FilterSelect
                  id="data-preview-object-kind-filter"
                  label={t("dataMgmt.preview.kindFilter")}
                  value={previewObjectKindFilter}
                  options={[
                    { value: "all", label: t("dataMgmt.preview.kindFilterAll") },
                    { value: "table", label: t("dataMgmt.preview.kindFilterTable") },
                    { value: "view", label: t("dataMgmt.preview.kindFilterView") },
                  ]}
                  onValueChange={setPreviewObjectKindFilter}
                />
              }
              activeFilterCount={isFilterActive(previewObjectKindFilter, "all") ? 1 : 0}
              onClearFilters={() => setPreviewObjectKindFilter("all")}
              onSearchChange={setPreviewObjectSearch}
              onOwnerPrefixChange={setPreviewObjectOwnerPrefix}
              onSortChange={(key) => setPreviewObjectSort((current) => nextPreviewObjectSort(current, key))}
              // 選んでいる行をもう一度押すとデータを取り直す（取消・失敗の後の読み直し）。
              onSelect={(name) => (name === previewObject ? void showPreview(name) : previewSelection.select(name))}
              onLoadMore={() => void previewObjectsQuery.fetchNextPage()}
              onRetryLoadMore={() => void previewObjectsQuery.fetchNextPage()}
              onRetry={() => void refreshObjects()}
              detailLabel={t("dataMgmt.preview.resultsTitle")}
              emptyDetail={
                <EmptyState title={t("dataMgmt.preview.emptyDetailTitle")} hint={t("dataMgmt.preview.emptyDetailHint")} />
              }
              detail={
                <PreviewResultsPanel
                  preview={preview}
                  loading={Boolean(previewLoadingObject)}
                  showButtonLoading={previewFromButton}
                  exporting={exportLoading}
                  previewError={previewError}
                  exportError={exportError}
                  rowLimitInput={previewRowLimitInput}
                  rowLimitError={previewRowLimitError}
                  executedRowLimit={executedPreviewRowLimit}
                  canShowPreview={canShowPreview}
                  selectedObjectName={previewObject}
                  selectedObject={selectedPreviewObjectItem}
                  truncateDisabled={Boolean(previewLoadingObject) || truncateLoading}
                  onRowLimitChange={updatePreviewRowLimitInput}
                  onShowPreview={() => void showPreview(previewObject, { fromButton: true })}
                  onRetryPreview={() => void showPreview(previewObject)}
                  onDownload={() => void downloadPreviewXlsx()}
                  onTruncateTable={openTruncateDialog}
                />
              }
            />
          </DbObjectManagementPanelShell>
        )}

        {activeView === "csv" && (
          <DbObjectManagementPanelShell
            id="data-management-panel-csv"
            labelledBy="data-management-tab-csv"
            idPrefix={DATA_MANAGEMENT_ID}
            ariaLabel={t("dataMgmt.workspace.csv")}
          >
            <CsvUploadWorkspace
              tablePickerItems={csvTablePickerItems}
              tableTotalCount={csvTableCount}
              tableUnfilteredTotalCount={baseObjectCounts?.tableCount}
              table={csvTable}
              tableSearch={csvTableSearch}
              tableSort={csvTableSort}
              filename={csvFilename}
              fileResetSignal={csvFileResetSignal}
              mode={csvMode}
              step={csvStep}
              confirmation={csvConfirmation}
              confirmed={csvConfirmed}
              canUpload={canUploadCsv}
              canClearUpload={canClearCsvUpload}
              result={csvUploadResult}
              loading={csvUploading}
              error={csvUploadError}
              tablesLoading={csvTablesQuery.isPending && !csvTablesQuery.data}
              tablesError={
                csvTablesErrorMessage
              }
              hasNextPage={Boolean(csvTablesQuery.hasNextPage)}
              loadingNextPage={csvTablesQuery.isFetchingNextPage}
              loadMoreError={csvTablesLoadMoreError}
              onTableSearchChange={setCsvTableSearch}
              onTableSortChange={(key) =>
                setCsvTableSort((current) => nextObjectPickerSort(current, key))
              }
              onTableChange={(value) => {
                csvTableManualSelection.current = true;
                setCsvTable(value);
                setCsvUploadResult(null);
                setCsvStep("file");
              }}
              onModeChange={(value) => {
                setCsvMode(value);
                setCsvUploadResult(null);
                setCsvStep("file");
              }}
              onFilePick={(file) => void pickCsvFile(file)}
              onFileClear={clearCsvFile}
              onClearUpload={clearCsvUpload}
              onConfirmationChange={(value) => {
                setCsvConfirmation(value);
                if (value.trim()) setCsvStep("execute");
              }}
              onUpload={() => void uploadCsv()}
              onRetry={() => void uploadCsv()}
              onTablesRetry={() => void csvTablesQuery.refetch()}
              onLoadMore={() => void csvTablesQuery.fetchNextPage()}
            />
          </DbObjectManagementPanelShell>
        )}

        {activeView === "synthetic" && (
          <DbObjectManagementPanelShell
            id="data-management-panel-synthetic"
            labelledBy="data-management-tab-synthetic"
            idPrefix={DATA_MANAGEMENT_ID}
            ariaLabel={t("dataMgmt.workspace.synthetic")}
          >
            {(selectAiProfilesQuery.isPending || selectAiProfilesQuery.error) && syntheticProgress}
            {selectAiProfilesQuery.isPending ? (
              <DbManagementLoadingSkeleton
                idPrefix="data-synthetic-profiles"
                ariaLabel={t("dataTools.syntheticData.profilesLoading")}
                variant="detail"
              />
            ) : selectAiProfilesQuery.error ? (
              <ErrorState
                message={apiErrorMessage(selectAiProfilesQuery.error, "dataMgmt.profiles.error")}
                onRetry={() => void selectAiProfilesQuery.refetch()}
              />
            ) : (
            <SyntheticWorkspace
              generationProgress={syntheticProgress}
              submitting={syntheticLoading === "generate"}
              selectAiDbProfiles={selectAiDbProfiles}
              selectedSyntheticProfile={selectedSyntheticProfile}
              syntheticData={syntheticData}
              syntheticDataResults={syntheticDataResults}
              syntheticProfileName={syntheticProfileName}
              syntheticAvailableTables={syntheticAvailableTables}
              syntheticResultTables={selectedRun ? selectedRun.targets.map((target) => target.table_name) : syntheticAvailableTables}
              syntheticSelectedTables={syntheticSelectedTables}
              syntheticPrompt={syntheticPrompt}
              syntheticConfirmation={syntheticConfirmation}
              syntheticDataConfirmed={syntheticDataConfirmed}
              canGenerateSyntheticData={canGenerateSyntheticData}
              syntheticCredentialPrecondition={syntheticCredentialPrecondition}
              canClearSyntheticGeneration={canClearSyntheticGeneration}
              syntheticRows={syntheticRows}
              syntheticSampleRows={syntheticSampleRows}
              syntheticUseComments={syntheticUseComments}
              syntheticResultTable={syntheticResultTable}
              syntheticResultLimitInput={syntheticResultLimitInput}
              syntheticResultLimitError={syntheticResultLimitError}
              executedSyntheticResultLimit={executedSyntheticResultLimit}
              canLoadSyntheticDataResults={canLoadSyntheticDataResults}
              canClearSyntheticDataResults={canClearSyntheticDataResults}
              loading={syntheticLoading}
              tablesButtonLoading={syntheticLoading === "tables" && syntheticTablesFromButton}
              error={syntheticWorkspaceError}
              resultError={syntheticResultError}
              dbProfileRefreshRequired={dbProfileRefreshRequired}
              dbProfileRefreshing={dbProfileRefreshing}
              dbProfileListSync={dbProfileListSync}
              onRefreshTables={() => void refreshSyntheticTables(true)}
              onSyntheticProfileNameChange={changeSyntheticProfileName}
              onSyntheticTableToggle={(tableName, selected) => {
                const nextTables = selected
                  ? uniqueStrings([...syntheticSelectedTables, tableName])
                  : syntheticSelectedTables.filter((item) => item !== tableName);
                setSyntheticSelectedTables(nextTables);
                setSyntheticResultTable((currentTable) =>
                  currentTable && nextTables.includes(currentTable) ? currentTable : (nextTables[0] ?? "")
                );
                setSyntheticData(null);
                clearSyntheticResultState();
              }}
              onSyntheticTablesBulkChange={(tableNames, selected) => {
                const targetSet = new Set(tableNames);
                const nextTables = selected
                  ? uniqueStrings([...syntheticSelectedTables, ...tableNames])
                  : syntheticSelectedTables.filter((item) => !targetSet.has(item));
                setSyntheticSelectedTables(nextTables);
                setSyntheticResultTable((currentTable) =>
                  currentTable && nextTables.includes(currentTable) ? currentTable : (nextTables[0] ?? "")
                );
                setSyntheticData(null);
                clearSyntheticResultState();
              }}
              onSyntheticPromptChange={setSyntheticPrompt}
              onSyntheticConfirmationChange={setSyntheticConfirmation}
              onSyntheticRowsChange={(value) => setSyntheticRows(clampNumber(value, 1, 100))}
              onSyntheticSampleRowsChange={(value) => setSyntheticSampleRows(clampNumber(value, 0, 100))}
              onSyntheticUseCommentsChange={setSyntheticUseComments}
              onSyntheticResultTableChange={(value) => {
                if (!(selectedRun ? selectedRun.targets.some((target) => target.table_name === value) : syntheticAvailableTables.includes(value))) return;
                resultSelectionEdited.current = true;
                setSyntheticResultTable(value);
                clearSyntheticResultState();
              }}
              onSyntheticResultLimitChange={(value) => {
                resultSelectionEdited.current = true;
                setSyntheticResultLimitInput(value);
                clearSyntheticResultState();
              }}
              onGenerateSyntheticData={() => void generateSyntheticData()}
              onClearSyntheticGeneration={clearSyntheticGeneration}
              onLoadSyntheticDataResults={() => void loadSyntheticDataResults()}
              onClearSyntheticDataResults={() => clearSyntheticResultState({ resetLimit: true })}
              onRetry={() => {
                if (syntheticErrorOperation === "tables") void refreshSyntheticTables();
                else if (syntheticErrorOperation === "results") void loadSyntheticDataResults();
                else void generateSyntheticData();
              }}
            />
            )}
            {syntheticDataResults && <Banner severity={syntheticDataResults.results.rows.length === 0 ? "warning" : "info"}>
              <p>{t(selectedRun?.preview ? "syntheticPreview.resultHint" : "syntheticRun.currentData")}</p>
              <p>{t("syntheticRun.queryTime", { time: formatDateTime(syntheticQueryTime) })}</p>
              {syntheticDataResults.results.rows.length === 0 && <p>{t(resultGeneratedRows === null ? "syntheticRun.unverifiedResult" : resultGeneratedRows > 0 ? "syntheticRun.mismatch" : "syntheticRun.zero")}</p>}
            </Banner>}
            {selectedRun?.preview && <SyntheticReview key={selectedRun.run_id} run={selectedRun} previews={viewedPreviews}
              stale={Boolean(syntheticRuns.error || historicalRun.error)}
              onUpdated={(updated) => {
                queryClient.setQueryData<SyntheticRun[]>(syntheticRuns.key, (old = []) => [updated, ...old.filter(item => item.run_id !== updated.run_id)]);
                if (updated.review_status === "discarded") clearSyntheticResultState();
              }} />}
          </DbObjectManagementPanelShell>
        )}
      </PageBody>
      {truncateTargetName && (
        <DropDbObjectDialog
          objectName={truncateTargetName}
          confirmation={truncateConfirmation}
          loading={truncateLoading}
          error={truncateError}
          labels={{
            title: t("dataMgmt.truncateDialog.title"),
            subtitle: t("dataMgmt.truncateDialog.subtitle"),
            close: t("dataMgmt.truncateDialog.close"),
            target: t("dataMgmt.truncateDialog.target"),
            executeTitle: t("dataMgmt.truncateDialog.executeTitle"),
            executeHint: t("dataMgmt.truncateDialog.executeHint"),
            cancel: t("dataMgmt.truncateDialog.cancel"),
            run: t("dataMgmt.truncate.action"),
          }}
          onConfirmationChange={(value) => {
            setTruncateConfirmation(value);
            if (truncateError) setTruncateError("");
          }}
          onExecute={() => void truncateTableData()}
          onClose={closeTruncateDialog}
        />
      )}
    </>
  );
}

function filterPreviewObjects(
  objects: DbAdminObjectSummary[],
  search: string,
  ownerPrefix: string,
  kindFilter: PreviewObjectKindFilter
) {
  const q = search.trim().toLowerCase();
  const ownerPrefixKey = ownerPrefix.trim().toUpperCase();
  return objects.filter((item) => {
    const kind: PreviewObjectKind = item.object_type === "view" ? "view" : "table";
    if (ownerPrefixKey && !item.owner.toUpperCase().startsWith(ownerPrefixKey)) return false;
    if (kindFilter !== "all" && kind !== kindFilter) return false;
    if (!q) return true;
    return [item.name, item.comment].join(" ").toLowerCase().includes(q);
  });
}

function apiErrorMessage(
  error: unknown,
  fallbackKey: Parameters<typeof t>[0],
  timeoutKey: Parameters<typeof t>[0] = "dataMgmt.operation.timeout",
  timeoutParams?: Record<string, string | number>
) {
  if (isTimeoutError(error)) return t(timeoutKey, timeoutParams);
  return error instanceof Error ? error.message : t(fallbackKey);
}

function objectListErrorMessage(error: unknown) {
  return apiErrorMessage(error, "dataMgmt.objectList.error", "dataMgmt.objectList.timeout", {
    seconds: requestTimeoutSeconds(API_TIMEOUT_MS.interactiveList),
  });
}

function objectListLoadMoreErrorMessage(error: unknown) {
  return apiErrorMessage(error, "dataMgmt.objectList.error", "objectSelector.loadMoreTimeout", {
    seconds: requestTimeoutSeconds(API_TIMEOUT_MS.interactiveList),
  });
}

function isSyntheticDataExecuted(result: SyntheticDataOperationData) {
  return result.executed === true || (result.status ?? "").toLowerCase() === "executed";
}

function previewObjectKindLabel(kind: PreviewObjectKind) {
  return kind === "view" ? t("dataMgmt.preview.kindFilterView") : t("dataMgmt.preview.kindFilterTable");
}

function previewObjectRowCountLabel(rowCount?: number | null) {
  if (rowCount == null) return t("dataMgmt.preview.rowUnknown");
  return t("dbAdmin.list.rows", { count: rowCount });
}

/**
 * データの表示の詳細の列（一覧 + 詳細の右の広い列。#2315）。上端に選んだ対象の見出しと対象の操作（XLSX・データを空にする）、
 * その下に取得件数上限と「データを表示」、データの結果は詳細の列の全幅に出す。枠は `ListDetailLayout` の詳細の列が持つ。
 */
function PreviewResultsPanel({
  preview,
  loading,
  showButtonLoading,
  exporting,
  previewError,
  exportError,
  rowLimitInput,
  rowLimitError,
  executedRowLimit,
  canShowPreview,
  selectedObjectName,
  selectedObject,
  truncateDisabled,
  onRowLimitChange,
  onShowPreview,
  onRetryPreview,
  onDownload,
  onTruncateTable,
}: {
  preview: DbAdminDataPreviewData | null;
  loading: boolean;
  /** 「表示」を押して始めたプレビューか（そのときだけボタンを回す。#819）。 */
  showButtonLoading: boolean;
  exporting: boolean;
  previewError: string;
  exportError: string;
  rowLimitInput: string;
  rowLimitError: string;
  executedRowLimit: number | null;
  canShowPreview: boolean;
  selectedObjectName: string;
  /** 一覧に読み込まれている選んだ対象（種類・行数・コメント）。一覧の外の対象（絞り込み・未読込）は undefined。 */
  selectedObject?: DbAdminObjectSummary;
  truncateDisabled?: boolean;
  onRowLimitChange: (value: string) => void;
  onShowPreview: () => void;
  onRetryPreview: () => void;
  onDownload: () => void;
  onTruncateTable: (objectName: string) => void;
}) {
  const selectedObjectKind: PreviewObjectKind | undefined = selectedObject
    ? selectedObject.object_type === "view"
      ? "view"
      : "table"
    : undefined;
  const showTruncateAction = Boolean(selectedObjectName) && selectedObjectKind === "table";
  const hasRowLimitError = Boolean(rowLimitError);
  const previewActions: EntityAction[] = [
    {
      id: "download-preview-xlsx",
      label: t("dataMgmt.preview.exportXlsx"),
      icon: FileSpreadsheet,
      loading: exporting,
      disabled: !preview || preview.results.rows.length === 0 || hasRowLimitError,
      onSelect: onDownload,
    },
    {
      id: "truncate-preview-table",
      label: t("dataMgmt.truncate.action"),
      ariaLabel: selectedObjectName
        ? t("dataMgmt.truncate.actionObject", { name: selectedObjectName })
        : t("dataMgmt.truncate.action"),
      icon: Trash2,
      tone: "danger",
      visible: showTruncateAction,
      disabled: truncateDisabled,
      onSelect: () => {
        if (selectedObjectName) onTruncateTable(selectedObjectName);
      },
    },
  ];

  return (
    <section
      className="grid min-w-0 content-start gap-4"
      aria-labelledby="data-preview-detail-heading"
      data-testid="data-preview-detail"
    >
      <div
        className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between"
        data-testid="data-preview-detail-header"
      >
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="data-preview-detail-heading" className="min-w-0">
              <DbObjectName value={selectedObjectName} size="base" />
            </h2>
            {selectedObjectKind && (
              <StatusBadge
                icon={false}
                variant={selectedObjectKind === "view" ? "info" : "neutral"}
                label={previewObjectKindLabel(selectedObjectKind)}
              />
            )}
            {selectedObject && (
              <StatusBadge icon={false} variant="neutral" label={previewObjectRowCountLabel(selectedObject.row_count)} />
            )}
          </div>
          {selectedObject?.comment && <p className="mt-2 text-sm leading-6 text-fg">{selectedObject.comment}</p>}
        </div>
        <ObjectActionBar
          actions={previewActions}
          ariaLabel={t("dataMgmt.preview.actions")}
          testId="data-preview-results-actions"
        />
      </div>
      {/* 取得件数上限は短い数値の幅（xs。#613）。操作ボタンはヘルパーテキストの下の行に置く。 */}
      <div className="grid grid-cols-1 gap-3 border-t border-border pt-3" data-testid="data-preview-controls">
        <RowLimitField
          value={rowLimitInput}
          onChange={onRowLimitChange}
          disabled={loading}
          error={rowLimitError}
        />
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
          <Button
            type="button"
            variant="primary"
            size="lg"
            className="w-full sm:w-auto"
            loading={loading && showButtonLoading}
            disabled={!canShowPreview && !(loading && showButtonLoading)}
            onClick={onShowPreview} icon={Play}>
            <span>{t("dataMgmt.preview.show")}</span>
          </Button>
        </div>
      </div>
      <section className="grid min-w-0 gap-2" aria-labelledby="data-preview-results-heading">
        <h3 id="data-preview-results-heading" className="text-sm font-semibold text-fg">
          {t("dataMgmt.preview.resultsTitle")}
        </h3>
        {loading ? (
          <DbManagementLoadingSkeleton
            idPrefix="data-preview-results"
            ariaLabel={t("dataMgmt.preview.loading")}
            variant="detail"
            // 「表示」を押したときは直上のボタンの loading がスピナーを出す（同じ処理のスピナーは 1 つ。
            // messaging §3.7、#416）。行の選択・切り詰めの後の自動の表示・「再試行」はこの表示が出す（#819）。
            activityIcon={showButtonLoading ? "none" : "spinner"}
          />
        ) : previewError ? (
          <ErrorState message={previewError} onRetry={onRetryPreview} />
        ) : preview ? (
          <div className="grid min-w-0 gap-2">
            {exportError && <ErrorState message={exportError} onRetry={onDownload} />}
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <StatusBadge icon={false} variant="neutral" label={runtimeLabel(preview.runtime)} />
              <span className="break-all font-mono text-xs text-fg-muted">{preview.sql}</span>
            </div>
            <WarningsBanner warnings={preview.warnings} />
            <QueryResultTable
              results={preview.results}
              rowLimit={executedRowLimit}
              name={t("queryResults.name.preview")}
              csvFilePrefix="nl2sql-data-preview"
            />
          </div>
        ) : (
          <>
            {exportError && <ErrorState message={exportError} onRetry={onDownload} />}
            <EmptyState title={t("dataMgmt.preview.emptyTitle")} hint={t("dataMgmt.preview.emptyHint")} />
          </>
        )}
      </section>
    </section>
  );
}

function CsvUploadWorkspace({
  tablePickerItems,
  tableTotalCount,
  tableUnfilteredTotalCount,
  table,
  tableSearch,
  tableSort,
  filename,
  fileResetSignal,
  mode,
  step,
  confirmation,
  confirmed,
  canUpload,
  canClearUpload,
  result,
  loading,
  error,
  tablesLoading,
  tablesError,
  hasNextPage,
  loadingNextPage,
  loadMoreError,
  onTableSearchChange,
  onTableSortChange,
  onTableChange,
  onModeChange,
  onFilePick,
  onFileClear,
  onClearUpload,
  onConfirmationChange,
  onUpload,
  onRetry,
  onTablesRetry,
  onLoadMore,
}: {
  tablePickerItems: DbObjectPickerItem[];
  tableTotalCount: number;
  /** 絞り込む前の表の全件（「N 件（全 M 件）」の M。まだ分からないときは undefined）。 */
  tableUnfilteredTotalCount?: number;
  table: string;
  tableSearch: string;
  tableSort: DbObjectPickerSortState;
  filename: string;
  fileResetSignal: number;
  mode: CsvMode;
  step: CsvStep;
  confirmation: string;
  confirmed: boolean;
  canUpload: boolean;
  canClearUpload: boolean;
  result: DbAdminCsvUploadData | null;
  loading: boolean;
  error: string;
  tablesLoading: boolean;
  tablesError: string;
  hasNextPage: boolean;
  loadingNextPage: boolean;
  loadMoreError: string;
  onTableSearchChange: (value: string) => void;
  onTableSortChange: (key: DbObjectPickerSortKey) => void;
  onTableChange: (value: string) => void;
  onModeChange: (value: CsvMode) => void;
  onFilePick: (file: File) => void;
  onFileClear: () => void;
  onClearUpload: () => void;
  onConfirmationChange: (value: string) => void;
  onUpload: () => void;
  onRetry: () => void;
  onTablesRetry: () => void;
  onLoadMore: () => void;
}) {
  const activeIndex = step === "execute" ? 1 : 0;
  const hasTableFilter = Boolean(tableSearch.trim());
  return (
    <fieldset disabled={loading} className="grid min-w-0 gap-4">
      <DbObjectPanelHeader
        icon={Upload}
        title={t("dataMgmt.csv.title")}
        description={t("dataMgmt.section.csvHint")}
      />

      <DbObjectStepIndicator
        steps={[t("dataMgmt.csv.stepFile"), t("dataMgmt.csv.stepExecute")]}
        activeIndex={activeIndex}
        ariaLabel={t("dataMgmt.csv.steps")}
        dataTestId="data-csv-steps"
      />

      {/* アップロード先の選択は検索・一覧・選択を束ねた複合入力なので fieldset にし、legend の「必須」を群の名前として読み上げる（#531）。 */}
      <fieldset
        className="grid min-w-0 gap-3"
        aria-describedby="data-csv-table-hint"
        data-testid="data-csv-table-section"
      >
        <FieldLegend id="data-csv-table-heading" required>
          {t("dataMgmt.csv.table")}
        </FieldLegend>
        <p id="data-csv-table-hint" className="mt-1 text-sm text-fg-muted">
          {t("dataMgmt.csv.tableHint")}
        </p>
        <DbObjectSelectionSummary label={t("objectSelector.selected")} value={table} />
        <DbObjectSelectorToolbar
          searchLabel={t("objectSelector.search")}
          searchPlaceholder={t("dataMgmt.csv.tableSearchPlaceholder")}
          searchValue={tableSearch}
          onSearchChange={onTableSearchChange}
          dataTestId="data-csv-table-toolbar"
          count={dbObjectListCount({
            matched: tableTotalCount,
            unfilteredTotal: tableUnfilteredTotalCount,
            filtered: hasTableFilter,
            ready: !tablesLoading && !tablesError,
          })}
        />
        {tablesLoading ? (
          <DbManagementLoadingSkeleton
            idPrefix="data-csv-table"
            ariaLabel={t("dataMgmt.objectList.loading")}
            variant="list"
            rows={5}
          />
        ) : tablesError ? (
          <ErrorState message={tablesError} onRetry={onTablesRetry} />
        ) : (
          <>
            <DbSingleObjectPickerList
              items={tablePickerItems}
              selectedKey={table}
              hasActiveFilter={hasTableFilter}
              listLabel={t("dataMgmt.csv.table")}
              emptyTitle={t("dataMgmt.csv.emptyTablesTitle")}
              emptyHint={t("dataMgmt.csv.emptyTablesHint")}
              noResultsTitle={t("dataMgmt.csv.noTablesTitle")}
              noResultsHint={t("dataMgmt.csv.noTablesHint")}
              dataTestId="data-csv-table-list"
              visibleRows={INFORMATION_TABLE_FIXED_VISIBLE_ROWS}
              sort={tableSort}
              onSortChange={onTableSortChange}
              onSelect={(item) => onTableChange(item.key)}
            />
            <DbObjectSelectorFooter
              visibleCount={tablePickerItems.length}
              totalCount={tableTotalCount}
              hasNextPage={hasNextPage}
              loadingNextPage={loadingNextPage}
              loadMoreError={loadMoreError}
              loadMoreLabel={t("dataMgmt.objectList.loadMore")}
              dataTestId="data-csv-table-footer"
              onLoadMore={onLoadMore}
              onRetryLoadMore={onLoadMore}
            />
          </>
        )}
      </fieldset>

      {error && <ErrorState message={error} onRetry={onRetry} />}

      {/* ファイル選択とアップロードモードは FormGrid（最大 60rem・置き場所 42rem 以上で 2 列。#1891）。 */}
      {/* モードの列は欄の幅（md）だけにし、残りをファイルの選択に使う（半分の幅ではファイル名が読めない）。 */}
      <FormGrid className="@2xl:grid-cols-[minmax(0,1fr)_auto]">
      <FileDropzone
        label={t("dataMgmt.csv.file")}
        accept={CORE_TABULAR_FILE_FORMATS.accept}
        selectedText={filename ? t("tableMgmt.importWizard.selectedFile", { filename }) : ""}
        formatLabel={CORE_TABULAR_FILE_FORMATS.formatLabel}
        resetSignal={fileResetSignal}
        actionText={t("common.fileDropzone.action")}
        replaceText={t("dataMgmt.csv.fileReplace")}
        clearAriaLabel={t("dataMgmt.csv.clearFile")}
        icon="spreadsheet"
        required
        dataTestId="data-csv-file-field"
        disabled={loading}
        onFiles={([file]) => onFilePick(file)}
        onClear={onFileClear}
      />

      <div className="min-w-0" data-testid="data-csv-mode-field">
        <SelectField<CsvMode>
          id="data-csv-mode"
          label={t("dataMgmt.csv.mode")}
          value={mode}
          options={[
            { value: "insert", label: t("dataMgmt.csv.mode.insert") },
            { value: "truncate_insert", label: t("dataMgmt.csv.mode.truncateInsert") },
          ]}
          // 「DELETE & INSERT(全置換)」が sm に収まらないので md（#1891）。
          width="md"
          onValueChange={onModeChange}
        />
      </div>
      </FormGrid>

      <fieldset
        className="grid gap-3 rounded-md border border-border bg-surface p-3"
        data-testid="data-csv-execution-fieldset"
      >
        <legend className="px-1 text-sm font-semibold text-fg">{t("dataMgmt.csv.executeTitle")}</legend>
        <ExecutionConfirmationField
          value={confirmation}
          onChange={onConfirmationChange}
          confirmed={confirmed}
          placeholder={table}
          expectedLabel={table || "-"}
          helper={t(
            mode === "truncate_insert"
              ? "dbAdmin.confirmation.helper.danger"
              : "dbAdmin.confirmation.helper.execute",
            { phrase: table || "-" }
          )}
          actions={
            <>
              <Button
                type="button"
                variant={mode === "truncate_insert" ? "danger" : "primary"}
                size="lg"
                className="w-full sm:w-auto"
                loading={loading}
                disabled={!canUpload}
                onClick={onUpload} icon={Upload}>
                <span>{t("dataMgmt.csv.upload")}</span>
              </Button>
              <ClearActionButton size="lg"
                label={t("dbAdmin.import.actions.clear")}
                className="w-full sm:w-auto"
                disabled={!canClearUpload || loading}
                onClick={onClearUpload}
              />
            </>
          }
        />
        {loading ? (
          // 行数が多い CSV は投入に数十秒かかる（messaging.md §3.7）。
          <ProcessingIndicator
            active
            label={t("dataMgmt.csv.progress.uploading")}
            operationKey="data-csv-upload"
            placement="action"
            activityIcon="none"
            testId="data-csv-upload-processing"
          />
        ) : null}
      </fieldset>

      {result && (
        <section className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3 text-sm" aria-label={t("dataMgmt.csv.result")}>
          <div className="flex flex-wrap gap-2">
            {/* 実行の状態・runtime・モードは API の内部値を出さず、文言で出す（#936）。 */}
            <StatusBadge
              variant={result.executed ? "success" : "warning"}
              label={t(result.executed ? "dbAdmin.result.summary.executed" : "dbAdmin.result.summary.notExecuted")}
            />
            <StatusBadge icon={false} variant="neutral" label={runtimeLabel(result.runtime)} />
            <StatusBadge icon={false} variant="neutral" label={csvModeLabel(result.mode)} />
            <StatusBadge icon={false} variant="info" label={t("tableMgmt.importWizard.rows", { count: result.row_count })} />
            {result.executed && (
              <>
                <StatusBadge variant="success" label={`${t("dataMgmt.csv.success")} ${result.success_count}`} />
                <StatusBadge
                  variant={result.error_count > 0 ? "danger" : "neutral"}
                  label={`${t("dataMgmt.csv.failed")} ${result.error_count}`}
                />
              </>
            )}
          </div>
          {!result.executed && result.warnings.some((warning) => warning.trim()) ? (
            // 実行されなかった（Oracle のエラー・実行環境）ときは失敗として出す。ORA のコード・Help は
            // 「詳細ログ」に畳む（messaging.md §10、#936）。
            <DbAdminErrorNotice
              error={new Error(`${t("dataMgmt.csv.failedSummary")}${result.warnings.filter((warning) => warning.trim()).join(" ")}`)}
            />
          ) : (
            <WarningsBanner warnings={result.warnings} />
          )}
          <p className="text-fg">
            {t("dataMgmt.csv.matched")}: <span className="font-mono text-xs">{result.matched_columns.join(", ") || "-"}</span>
          </p>
          {result.unmatched_csv_columns.length > 0 && (
            <p className="text-fg">
              {t("dataMgmt.csv.unmatched")}: <span className="font-mono text-xs">{result.unmatched_csv_columns.join(", ")}</span>
            </p>
          )}
          {result.row_errors.length > 0 && (
            <div className="grid gap-1">
              <p className="font-semibold text-fg">{t("dataMgmt.csv.rowErrors")}</p>
              {result.row_errors.map((error) => (
                <p key={error} data-surface-tint="danger" className="rounded-md border border-danger-border bg-danger-subtle px-3 py-2 text-danger-fg">
                  {error}
                </p>
              ))}
            </div>
          )}
          {result.hint && (
            <p data-surface-tint="accent" className="rounded-md border border-accent-emphasis bg-accent-subtle px-3 py-2 text-accent-fg">{result.hint}</p>
          )}
          {result.sample_rows.length > 0 && (
            <div className="grid gap-1">
              <p className="font-semibold text-fg">{t("dataMgmt.csv.preview")}</p>
              <QueryResultTable
                results={{
                  columns: Object.keys(result.sample_rows[0] ?? {}),
                  rows: result.sample_rows,
                  total: result.sample_rows.length,
                }}
                name={t("queryResults.name.importPreview")}
                csvFilePrefix="nl2sql-csv-import-preview"
                testId="csv-import-preview"
              />
            </div>
          )}
        </section>
      )}
    </fieldset>
  );
}

function csvModeLabel(mode: string) {
  if (mode === "insert") return t("dataMgmt.csv.mode.insert");
  if (mode === "truncate_insert") return t("dataMgmt.csv.mode.truncateInsert");
  return mode;
}

function SyntheticWorkspace({
  generationProgress,
  submitting,
  selectAiDbProfiles,
  selectedSyntheticProfile,
  syntheticData,
  syntheticDataResults,
  syntheticProfileName,
  syntheticAvailableTables,
  syntheticResultTables,
  syntheticSelectedTables,
  syntheticPrompt,
  syntheticConfirmation,
  syntheticDataConfirmed,
  canGenerateSyntheticData,
  syntheticCredentialPrecondition,
  canClearSyntheticGeneration,
  syntheticRows,
  syntheticSampleRows,
  syntheticUseComments,
  syntheticResultTable,
  syntheticResultLimitInput,
  syntheticResultLimitError,
  executedSyntheticResultLimit,
  canLoadSyntheticDataResults,
  canClearSyntheticDataResults,
  loading,
  tablesButtonLoading,
  error,
  resultError,
  dbProfileRefreshRequired,
  dbProfileRefreshing,
  dbProfileListSync,
  onRefreshTables,
  onSyntheticProfileNameChange,
  onSyntheticTableToggle,
  onSyntheticTablesBulkChange,
  onSyntheticPromptChange,
  onSyntheticConfirmationChange,
  onSyntheticRowsChange,
  onSyntheticSampleRowsChange,
  onSyntheticUseCommentsChange,
  onSyntheticResultTableChange,
  onSyntheticResultLimitChange,
  onGenerateSyntheticData,
  onClearSyntheticGeneration,
  onLoadSyntheticDataResults,
  onClearSyntheticDataResults,
  onRetry,
}: {
  generationProgress: ReactNode;
  submitting: boolean;
  selectAiDbProfiles: SelectAiDbProfilesData | null;
  selectedSyntheticProfile: SelectAiDbProfile | null;
  syntheticData: SyntheticDataOperationData | null;
  syntheticDataResults: SyntheticDataResultsData | null;
  syntheticProfileName: string;
  syntheticAvailableTables: string[];
  syntheticResultTables: string[];
  syntheticSelectedTables: string[];
  syntheticPrompt: string;
  syntheticConfirmation: string;
  syntheticDataConfirmed: boolean;
  canGenerateSyntheticData: boolean;
  /** Select AI の Credential が無いときの事前条件の案内（#1688）。無ければ null。 */
  syntheticCredentialPrecondition: ReactNode;
  canClearSyntheticGeneration: boolean;
  syntheticRows: number;
  syntheticSampleRows: number;
  syntheticUseComments: boolean;
  syntheticResultTable: string;
  syntheticResultLimitInput: string;
  syntheticResultLimitError: string;
  executedSyntheticResultLimit: number | null;
  canLoadSyntheticDataResults: boolean;
  canClearSyntheticDataResults: boolean;
  loading: SyntheticLoading;
  /** 「テーブル再取得」を押して始めた取得の間だけ true（#819）。 */
  tablesButtonLoading: boolean;
  error: string;
  resultError: string;
  dbProfileRefreshRequired: boolean;
  dbProfileRefreshing: boolean;
  /** DB Profile 一覧の再取得（案内と進行は「対象」の欄の 1 か所。#2034）。 */
  dbProfileListSync: DbProfileListSync;
  onRefreshTables: () => void;
  onSyntheticProfileNameChange: (value: string) => void;
  onSyntheticTableToggle: (tableName: string, selected: boolean) => void;
  onSyntheticTablesBulkChange: (tableNames: string[], selected: boolean) => void;
  onSyntheticPromptChange: (value: string) => void;
  onSyntheticConfirmationChange: (value: string) => void;
  onSyntheticRowsChange: (value: number) => void;
  onSyntheticSampleRowsChange: (value: number) => void;
  onSyntheticUseCommentsChange: (value: boolean) => void;
  onSyntheticResultTableChange: (value: string) => void;
  onSyntheticResultLimitChange: (value: string) => void;
  onGenerateSyntheticData: () => void;
  onClearSyntheticGeneration: () => void;
  onLoadSyntheticDataResults: () => void;
  onClearSyntheticDataResults: () => void;
  onRetry: () => void;
}) {
  const generationSucceeded = syntheticData !== null && isSyntheticDataExecuted(syntheticData);
  const activeStep = generationSucceeded || syntheticDataResults ? 1 : 0;
  const [syntheticTableSearch, setSyntheticTableSearch] = useState("");
  // 件数の欄は入力中の文字を欄で持ち、範囲内の整数のときだけ値を変える（空にしても即 1 / 0 にしない。#1103）。
  const syntheticRowsDraft = useNumberDraft(syntheticRows, {
    min: 1,
    max: 100,
    integer: true,
    onChange: onSyntheticRowsChange,
  });
  const syntheticSampleRowsDraft = useNumberDraft(syntheticSampleRows, {
    min: 0,
    max: 100,
    integer: true,
    onChange: onSyntheticSampleRowsChange,
  });
  // 親の syntheticDataConfirmed と同じ規則(単一テーブル=対象名 / 複数=ADMIN_EXECUTE)。
  const syntheticExpectedConfirmation =
    syntheticSelectedTables.length === 1 ? syntheticSelectedTables[0] : "ADMIN_EXECUTE";
  const resultTableOptions = syntheticResultTables;
  const hasValidResultTable = resultTableOptions.includes(syntheticResultTable);
  const normalizedSyntheticTableSearch = syntheticTableSearch.trim().toLowerCase();
  const filteredSyntheticTables = normalizedSyntheticTableSearch
    ? syntheticAvailableTables.filter((tableName) =>
        tableName.toLowerCase().includes(normalizedSyntheticTableSearch)
      )
    : syntheticAvailableTables;
  const selectedSyntheticTableSet = new Set(syntheticSelectedTables);
  // 一覧の再取得を求める状態は構造化した項目（`profile_list_refresh_required`）から案内に出す。backend は同じ文を
  // warnings に入れない（#2034）。
  const visibleWarnings = selectAiDbProfiles?.warnings ?? [];

  return (
    <div className="grid gap-4">
      <DbObjectPanelHeader
        icon={Database}
        title={t("dataTools.synthetic.title")}
        description={t("dataMgmt.section.syntheticHint")}
      />
      {error && <ErrorState message={error} onRetry={onRetry} />}
      <DbObjectStepIndicator
        steps={[
          t("dataTools.syntheticData.stepTarget"),
          t("dataTools.syntheticData.stepResults"),
        ]}
        activeIndex={activeStep}
        ariaLabel={t("dataTools.syntheticData.steps")}
        dataTestId="data-synthetic-steps"
      />

      <fieldset disabled={loading === "generate"} className="grid min-w-0 gap-3 rounded-md border border-border bg-surface-sunken p-3" aria-labelledby="synthetic-target-heading">
        <DbObjectPanelHeader
          headingId="synthetic-target-heading"
          icon={Database}
          title={t("dataTools.syntheticData.stepTarget")}
          description={t("dataTools.syntheticData.targetHint")}
        />

        {dbProfileRefreshRequired || dbProfileListSync.view ? (
          <DbProfileListSyncNotice
            sync={dbProfileListSync}
            testId="data-synthetic-db-profile-refresh-notice"
            actionTestId="data-synthetic-db-profile-refresh"
            processingTestId="data-synthetic-db-profile-refresh-processing"
            actions={
              <ButtonLink
                to={APP_ROUTES.profiles}
                linkComponent={Link}
                variant="secondary"
                size="sm"
                trailingIcon={ArrowRight}
                className="w-full sm:w-auto"
              >
                {t("dataTools.syntheticData.openProfileManagement")}
              </ButtonLink>
            }
          />
        ) : null}

        {/* Select AI のプロファイルは対象の選択なので lg、1 表あたりの行数は数値なので xs（#1891）。 */}
        <FormGrid>
          {/* 生成とテーブル取得は DB Profile の選択が前提（canGenerateSyntheticData）なので必須として示す（#531）。 */}
          <SelectField
            id="data-synthetic-profile"
            label={t("dataTools.syntheticData.profile")}
            required
            value={syntheticProfileName}
            placeholder={(selectAiDbProfiles?.profiles ?? []).length === 0 ? t("dataTools.syntheticData.noProfiles") : ""}
            options={(selectAiDbProfiles?.profiles ?? []).map((profile) => ({ value: profile.name, label: profile.name }))}
            onValueChange={onSyntheticProfileNameChange}
            disabled={dbProfileRefreshRequired || dbProfileRefreshing}
            width="lg"
            className="min-w-0"
          />
          <TextField
            id="data-synthetic-rows-per-table"
            label={t("dataTools.syntheticData.rowsPerTable")}
            type="number"
            min={1}
            max={100}
            {...syntheticRowsDraft}
            width="xs"
            className="min-w-0"
          />
        </FormGrid>

        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge
            icon={false}
            variant={syntheticSelectedTables.length > 0 ? "info" : "neutral"}
            label={t("dataTools.syntheticData.selectedCount", { count: syntheticSelectedTables.length })}
          />
          {selectedSyntheticProfile?.owner && <StatusBadge icon={false} variant="neutral" label={selectedSyntheticProfile.owner} />}
          {selectedSyntheticProfile?.status && <StatusBadge icon={false} variant="neutral" label={selectedSyntheticProfile.status} />}
        </div>
        <WarningsBanner warnings={visibleWarnings} />

        <ContentActionBar
          ariaLabel={t("dataTools.syntheticData.refreshTablesActions")}
          title={t("dataTools.syntheticData.refreshTablesActionTitle")}
          description={
            syntheticProfileName
              ? t("dataTools.syntheticData.refreshTablesActionReady")
              : t("dataTools.syntheticData.refreshTablesActionDisabled")
          }
          actionsClassName="w-full sm:w-auto"
          testId="data-synthetic-refresh-tables-actions"
        >
          <Button
            type="button"
            variant="primary"
            size="lg"
            className="w-full sm:w-auto"
            // 押した取得の間だけ回す。失敗の「再試行」からの取得では無効にするだけ（候補の一覧の読込表示が出す。#819）。
            loading={tablesButtonLoading}
            disabled={
              !syntheticProfileName ||
              dbProfileRefreshRequired ||
              dbProfileRefreshing ||
              (loading === "tables" && !tablesButtonLoading)
            }
            onClick={onRefreshTables} icon={RefreshCw}>
            <span>{t("dataTools.syntheticData.refreshTables")}</span>
          </Button>
        </ContentActionBar>

        {/* 対象の表は業務プロファイルの許可する表（数百件になりうる）。複数を選ぶので、大量の候補から選ぶ共通の
            ListPicker にする（#608）。候補はプロファイルから全件を読んでいるので、検索は画面側で絞る。 */}
        <ListPicker
          id="data-synthetic-table-picker"
          label={t("dataTools.syntheticData.tables")}
          items={filteredSyntheticTables.map(syntheticTablePickerItem)}
          selectedKeys={selectedSyntheticTableSet}
          selectedItems={syntheticSelectedTables.map(syntheticTablePickerItem)}
          onToggle={(item, selected) => onSyntheticTableToggle(item.key, selected)}
          onSelectMany={(visible) => onSyntheticTablesBulkChange(visible.map((item) => item.key), true)}
          onClearSelection={() => onSyntheticTablesBulkChange(syntheticSelectedTables, false)}
          total={filteredSyntheticTables.length}
          search={{
            id: "data-synthetic-table-search",
            label: t("dataTools.syntheticData.tables"),
            placeholder: t("dataTools.syntheticData.tableSearchPlaceholder"),
            value: syntheticTableSearch,
            onSearch: setSyntheticTableSearch,
            clearLabel: t("common.clearSearch"),
          }}
          loading={loading === "tables"}
          fixedHeight
          labels={{
            resultCount: ({ visible, selected }) =>
              t("objectSelector.resultCountWithSelected", {
                visible,
                total: syntheticAvailableTables.length,
                selected,
              }),
            loading: t("dataTools.syntheticData.tablesLoading"),
            emptyTitle: t("dataTools.syntheticData.noTablesTitle"),
            emptyHint: t("dataTools.syntheticData.noTablesHint"),
            noResultsTitle: t("dataTools.syntheticData.noTableResultsTitle"),
            noResultsHint: t("dataTools.syntheticData.noTableResultsHint"),
            clearSearch: t("common.clearSearch"),
            keyboardHint: t("objectSelector.keyboardHint"),
          }}
          testId="data-synthetic-table"
        />

        {/* 指示（文章）は full で 2 列にまたがせ、オプションはその下（#1891）。placeholder は短い例、説明は helper。 */}
        <FormGrid>
          <TextareaField
            id="data-synthetic-prompt"
            label={t("dataTools.syntheticData.prompt")}
            className="col-span-full min-w-0"
            value={syntheticPrompt}
            onChange={(event) => onSyntheticPromptChange(event.currentTarget.value)}
            rows={5}
            width="full"
            placeholder={t("dataTools.syntheticData.promptPlaceholder")}
            helper={t("dataTools.syntheticData.promptHelper")}
          />
          <fieldset className="grid content-start gap-3 rounded-md border border-border bg-surface p-3">
            <legend className="px-1 text-sm font-semibold text-fg">{t("dataTools.syntheticData.options")}</legend>
            <TextField
              id="data-synthetic-sample-rows"
              label={t("dataTools.syntheticData.sampleRows")}
              type="number"
              min={0}
              max={100}
              {...syntheticSampleRowsDraft}
              width="xs"
            />
            <label className="flex min-h-11 items-center gap-2 rounded-md border border-border bg-surface-sunken px-3 py-2 text-sm font-medium text-fg">
              <input
                type="checkbox"
                checked={syntheticUseComments}
                onChange={(event) => onSyntheticUseCommentsChange(event.currentTarget.checked)}
                className="h-4 w-4 rounded border-border text-accent-fg"
              />
              <span>{t("dataTools.syntheticData.useComments")}</span>
            </label>
          </fieldset>
        </FormGrid>

        <fieldset className="grid gap-3 rounded-md border border-border bg-surface p-3">
          <legend className="px-1 text-sm font-semibold text-fg">{t("dataTools.syntheticData.executeTitle")}</legend>
          {syntheticCredentialPrecondition}
          <ExecutionConfirmationField
            value={syntheticConfirmation}
            onChange={onSyntheticConfirmationChange}
            confirmed={syntheticDataConfirmed}
            placeholder={syntheticExpectedConfirmation}
            expectedLabel={syntheticExpectedConfirmation}
            helper={t("dbAdmin.confirmation.helper.danger", {
              phrase: syntheticExpectedConfirmation,
            })}
            actions={
              <>
                <Button
                  type="button"
                  variant="danger"
                  size="lg"
                  className="w-full sm:w-auto"
                  loading={submitting}
                  disabled={!canGenerateSyntheticData || dbProfileRefreshRequired || dbProfileRefreshing}
                  aria-describedby={
                    syntheticCredentialPrecondition ? SYNTHETIC_CREDENTIAL_PRECONDITION_ID : undefined
                  }
                  onClick={onGenerateSyntheticData} icon={Database}>
                  <span>{t("dataTools.syntheticData.generate")}</span>
                </Button>
                <ClearActionButton size="lg"
                  label={t("dataTools.syntheticData.actions.clear")}
                  className="w-full sm:w-auto"
                  disabled={
                    !canClearSyntheticGeneration ||
                    Boolean(loading) ||
                    dbProfileRefreshRequired ||
                    dbProfileRefreshing
                  }
                  onClick={onClearSyntheticGeneration}
                />
              </>
            }
          />
        </fieldset>

      </fieldset>

      {generationProgress}

      <section className="grid min-w-0 content-start gap-3 rounded-md border border-border bg-surface-sunken p-4" aria-labelledby="synthetic-results-heading">
        <DbObjectPanelHeader
          headingId="synthetic-results-heading"
          icon={Eye}
          title={t("dataTools.syntheticData.resultsActionTitle")}
          description={t("dataTools.syntheticData.resultsActionDisabled")}
        />

        {/* 結果テーブル（表名なので md）と取得件数上限（xs）は FormGrid、ボタン行はその下の全幅（#1891）。 */}
        <div className="border-t border-border pt-3">
        <FormGrid>
          <SelectField
            id="data-synthetic-result-table"
            label={t("dataTools.syntheticData.resultTable")}
            data-testid="synthetic-result-table-select"
            value={hasValidResultTable ? syntheticResultTable : ""}
            placeholder={resultTableOptions.length === 0 ? t("dataTools.syntheticData.noResultTables") : ""}
            options={resultTableOptions.map((tableName) => ({ value: tableName, label: tableName }))}
            onValueChange={onSyntheticResultTableChange}
            disabled={loading === "results"}
            width="md"
            className="min-w-0"
          />
          <RowLimitField
            value={syntheticResultLimitInput}
            onChange={onSyntheticResultLimitChange}
            disabled={loading === "results"}
            error={syntheticResultLimitError}
          />
          <div
            className="col-span-full flex flex-col gap-2 sm:flex-row sm:flex-wrap"
            role="group"
            aria-label={t("dataTools.syntheticData.resultsActions")}
            data-testid="data-synthetic-results-actions"
          >
            <Button
              type="button"
              variant="primary"
              size="lg"
              className="w-full sm:w-auto"
              loading={loading === "results"}
              disabled={!canLoadSyntheticDataResults}
              onClick={onLoadSyntheticDataResults} icon={Eye}>
              <span>{t("dataTools.syntheticData.results")}</span>
            </Button>
            <Button
              type="button"
              variant="secondary"
              size="lg"
              className="w-full sm:w-auto"
              disabled={!canClearSyntheticDataResults || loading === "results"}
              onClick={onClearSyntheticDataResults} icon={X}>
              <span>{t("dataTools.syntheticData.clearResults")}</span>
            </Button>
          </div>
        </FormGrid>
        </div>

        {loading === "results" ? (
          <DbManagementLoadingSkeleton
            idPrefix="data-synthetic-results"
            ariaLabel={t("dataTools.syntheticData.resultsLoading")}
            variant="detail"
            placement="result"
          />
        ) : resultError ? (
          <ErrorState message={resultError} onRetry={onLoadSyntheticDataResults} />
        ) : syntheticDataResults ? (
          <div className="grid min-w-0 gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <DbObjectName value={syntheticDataResults.table_name} size="sm" />
              <StatusBadge icon={false} variant="neutral" label={syntheticDataResults.runtime} />
            </div>
            <WarningsBanner warnings={syntheticDataResults.warnings} />
            <QueryResultTable
              results={syntheticDataResults.results}
              rowLimit={executedSyntheticResultLimit}
              name={t("queryResults.name.synthetic")}
              csvFilePrefix="nl2sql-synthetic-data"
            />
          </div>
        ) : (
          <EmptyState title={t("dataTools.syntheticData.noResultsTitle")} hint={t("dataTools.syntheticData.noResultsHint")} />
        )}
      </section>
    </div>
  );
}

/** 合成データの対象の表の候補の 1 行（名前は SQL と同じ表記）。 */
function syntheticTablePickerItem(tableName: string): ListPickerItem {
  return {
    key: tableName,
    label: <DbObjectName value={tableName} size="xs" truncate className="block" />,
    textValue: tableName,
  };
}

async function previewExportError(response: Response) {
  try {
    const payload = (await response.json()) as { detail?: unknown; error?: unknown };
    return String(payload.error || payload.detail || t("dataMgmt.error.previewExport"));
  } catch {
    return t("dataMgmt.error.previewExport");
  }
}

function profileObjectNames(profile: SelectAiDbProfile | null | undefined) {
  if (!profile) return [];
  const rawItems = [
    ...collectProfileObjectListItems(profile.object_list, true),
    ...collectProfileObjectListItems(profile.attributes, false),
  ];
  return uniqueStrings(
    rawItems.flatMap((item) => {
      const name = profileObjectName(item);
      return name ? [name] : [];
    })
  );
}

function collectProfileObjectListItems(value: unknown, candidateScope: boolean, depth = 0): unknown[] {
  if (value === null || value === undefined || depth > 6) return [];
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return [];
    try {
      return collectProfileObjectListItems(JSON.parse(trimmed) as unknown, candidateScope, depth + 1);
    } catch {
      return candidateScope ? [trimmed] : [];
    }
  }
  if (Array.isArray(value)) {
    return value.flatMap((item) => collectProfileObjectListItems(item, true, depth + 1));
  }
  if (!isRecord(value)) return [];
  if (candidateScope && profileObjectName(value)) return [value];

  const rawItems: unknown[] = [];
  for (const key of ["object_list", "OBJECT_LIST", "objectList", "objects", "OBJECTS", "tables", "TABLES"]) {
    if (key in value) {
      rawItems.push(...collectProfileObjectListItems(value[key], true, depth + 1));
    }
  }
  for (const key of [
    "attributes",
    "ATTRIBUTES",
    "profile_attributes",
    "PROFILE_ATTRIBUTES",
    "profileAttributes",
    "params",
    "PARAMS",
  ]) {
    if (key in value) {
      rawItems.push(...collectProfileObjectListItems(value[key], false, depth + 1));
    }
  }
  return rawItems;
}

function profileObjectName(item: unknown) {
  if (typeof item === "string") return item.trim();
  if (!isRecord(item)) return "";
  const owner = item.owner ?? item.OWNER;
  const name =
    item.name ??
    item.NAME ??
    item.object_name ??
    item.OBJECT_NAME ??
    item.table_name ??
    item.TABLE_NAME ??
    item.objectName ??
    item.tableName;
  if (typeof name !== "string") return "";
  const trimmedName = name.trim();
  if (!trimmedName) return "";
  const trimmedOwner = typeof owner === "string" ? owner.trim() : "";
  // profile の object_list は利用者の入力値のため、backend と同じ入力規則で正規化する
  // （引用符のない名前は大文字化）。owner が無い場合は名前だけにし、推測で補わない。
  try {
    return trimmedOwner
      ? parseDbAdminObjectTarget(trimmedName, trimmedOwner).qualifiedName
      : formatDbAdminObjectPart(trimmedName);
  } catch {
    return trimmedOwner ? `${trimmedOwner}.${trimmedName}` : trimmedName;
  }
}

function uniqueStrings(values: string[]) {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].sort((left, right) =>
    compareNames(left, right)
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function clampNumber(value: number, min: number, max: number) {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}
