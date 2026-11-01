import { WarningsBanner } from "@/components/WarningsBanner";
import { useWorkspaceActive, useWorkspaceState, useWorkspaceRevalidation, useResetExecutionConsent, useTransientDraftGuard, useWorkspaceUrlSelection } from "@/components/WorkspaceState";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { FileSpreadsheet, Play, RefreshCw, Table2, Trash2, Upload } from "lucide-react";

import {
  Button,
  EmptyState,
  toast,
  StatusBadge,
  PageHeader,
  PageBody,
  ClearActionButton,
  ProcessingIndicator,
  ObjectActionBar,
  type EntityAction,
  INFORMATION_TABLE_FIXED_VISIBLE_ROWS,
  ExecutionConfirmationField,
  FieldLegend,
  FormGrid,
  SelectField,
  useActionPending,
  FilterSelect,
  isFilterActive,
} from "@production-ready/ui";


import { ErrorState } from "@/components/StateViews";
import { FileDropzone } from "@/components/ui/file-dropzone";
import { apiFetch, apiPost, isTimeoutError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/i18n";
import { API_TIMEOUT_MS, requestTimeoutSeconds } from "@/lib/requestPolicy";
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
import type {
  DbAdminCsvUploadData,
  DbAdminDataPreviewData,
  DbAdminExecuteData,
  DbAdminObjectSummary,
} from "../types";

type ActiveView = "preview" | "csv";
type CsvStep = "file" | "execute";
type CsvMode = "insert" | "truncate_insert";
type PreviewObjectKind = "table" | "view";
type PreviewObjectKindFilter = "all" | PreviewObjectKind;

const DATA_MANAGEMENT_ID = "data-management";
/** ヘッダーの「スキーマを更新」の起点（SchemaRefreshCoordinator の start(origin)。#821）。 */
const DATA_SCHEMA_REFRESH_HEADER = "data-management-header";
/** 切り詰めの後の案内の「DB 構造を再取得」の起点（#2031）。 */
const DATA_SCHEMA_REFRESH_NOTICE = "data-management-notice";
const DEFAULT_DATA_PREVIEW_ROW_LIMIT = DEFAULT_SQL_ROW_LIMIT;
const DEFAULT_OBJECT_PICKER_SORT: DbObjectPickerSortState = { key: "name", direction: "asc" };
const DEFAULT_PREVIEW_OBJECT_SORT: DbObjectSortState = { key: "name", direction: "asc" };
/**
 * データの表示で選んだ対象（`OWNER.NAME`）の URL の検索パラメータ（一覧 + 詳細。#2315）。
 * タブの 1 つだけが一覧 + 詳細なので、ほかのタブへ移るときに URL から外す（タブへ戻ると最後の選択を戻す）。
 */
const PREVIEW_OBJECT_PARAM = "object";

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

export function DataManagementPage() {
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
  const [previewLoadingObject, setPreviewLoadingObject] = useState("");
  // 「表示」を押して始めたプレビューか。切り詰めの後の自動の表示・「再試行」ではボタンを回さず、
  // 結果の領域の読込表示がスピナーを出す（#819）。
  const [previewFromButton, setPreviewFromButton] = useState(false);
  const [truncateTargetName, setTruncateTargetName] = useState("");
  const [truncateConfirmation, setTruncateConfirmation] = useState("");
  const [truncateLoading, setTruncateLoading] = useState(false);
  const [truncateError, setTruncateError] = useState("");
  useResetExecutionConsent(() => { setCsvConfirmation(""); setTruncateConfirmation(""); setTruncateTargetName(""); }, JSON.stringify([csvTable, csvBase64, csvMode]));
  const [previewError, setPreviewError] = useState("");
  const [exportLoading, setExportLoading] = useState(false);
  const [exportError, setExportError] = useState("");
  const [csvUploading, setCsvUploading] = useState(false);
  const [csvUploadError, setCsvUploadError] = useState("");
  // ヘッダーの「最新情報を取得」を押した取り直しの間だけ true（切り詰め・取込の後の取り直しでは回さない。#819）。
  const manualRefresh = useActionPending();
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

  // 対象名確認: CSV は選択テーブル名。backend の検証と一致させる。
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
async function previewExportError(response: Response) {
  try {
    const payload = (await response.json()) as { detail?: unknown; error?: unknown };
    return String(payload.error || payload.detail || t("dataMgmt.error.previewExport"));
  } catch {
    return t("dataMgmt.error.previewExport");
  }
}
